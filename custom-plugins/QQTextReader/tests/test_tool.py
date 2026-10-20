from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from langbot_plugin.api.entities.builtin.platform.message import File, MessageChain

from components.tools import read_text_attachment as module
from pkg import text_context


@pytest.fixture
def tool_env(monkeypatch):
    monkeypatch.setattr(text_context, "_CACHE", text_context.AttachmentCache())
    variables, platform_calls = {}, []

    async def platform_api(*args):
        platform_calls.append(args)
        return {"url": "https://public.example/signed?secret=hidden"}

    plugin = SimpleNamespace(
        plugin_runtime_handler=object(), call_platform_api=platform_api
    )

    class API:
        def __init__(self, **kwargs):
            assert kwargs["query_id"] == 1

        async def get_query_vars(self):
            return variables

        async def get_bot_uuid(self):
            return "test-bot"

    monkeypatch.setattr(module, "QueryBasedAPIProxy", API)
    tool = module.ReadTextAttachment()
    tool.plugin = plugin

    def capture(file=None, kind="group"):
        source = SimpleNamespace(
            bot_uuid="test-bot",
            launcher_type=kind,
            launcher_id="123",
            sender_id="456",
            message_chain=MessageChain([file] if file else []),
        )
        variables[text_context.QUERY_CONTEXT_KEY] = text_context.capture_context(
            plugin, source
        )

    return tool, capture, variables, platform_calls


@pytest.mark.asyncio
async def test_current_and_recent_inline_exact_text(tool_env):
    tool, capture, _, calls = tool_env
    raw = "离线合成测试,第一行.\n第二行。".encode("utf-8-sig")
    capture(
        File(name="offline.txt", size=len(raw), base64=base64.b64encode(raw).decode())
    )
    capture()
    result = json.loads(await tool.call({}, None, 1))
    assert result == {
        "name": "offline.txt",
        "encoding": "utf-8-sig",
        "text": raw.decode("utf-8-sig"),
        "complete": True,
    }
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group", "person"])
async def test_missing_url_uses_fixed_platform_api(tool_env, monkeypatch, kind):
    tool, capture, _, calls = tool_env
    capture(File(id="opaque-file", name="offline.txt", size=3), kind)

    async def download(url):
        assert url == "https://public.example/signed?secret=hidden"
        return b"abc"

    monkeypatch.setattr(module, "download", download)
    result = await tool.call({}, None, 1)
    payload = {"file_id": "opaque-file"}
    action = "get_private_file_url"
    if kind == "group":
        payload["group_id"] = "123"
        action = "get_group_file_url"
    assert calls == [
        ("test-bot", "call_platform_api", {"action": action, "params": payload})
    ]
    assert json.loads(result)["text"] == "abc"
    assert all(
        secret not in result
        for secret in ["opaque-file", "https://", "123", "456", "secret"]
    )


@pytest.mark.asyncio
async def test_existing_url_never_calls_platform_api(tool_env, monkeypatch):
    tool, capture, _, calls = tool_env
    capture(File(id="opaque", name="a.txt", url="https://public.example/file"))

    async def download(url):
        return b"abc"

    monkeypatch.setattr(module, "download", download)
    assert json.loads(await tool.call({}, None, 1))["complete"] is True
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [{"url": "http://127.0.0.1"}, {"path": "C:\\secret"}, {"file_id": "other"}, None],
)
async def test_tool_rejects_model_supplied_target(tool_env, params):
    tool, _, _, calls = tool_env
    assert json.loads(await tool.call(params, None, 1))["complete"] is False
    assert calls == []


@pytest.mark.asyncio
async def test_missing_context_or_restart_is_not_success(tool_env, monkeypatch):
    tool, capture, _, calls = tool_env
    assert json.loads(await tool.call({}, None, 1))["complete"] is False
    capture(File(name="a.txt", base64="YQ=="))
    monkeypatch.setattr(text_context, "_CACHE", text_context.AttachmentCache())
    assert json.loads(await tool.call({}, None, 1))["complete"] is False
    assert calls == []


@pytest.mark.asyncio
async def test_provider_failure_never_leaks_private_data(tool_env, monkeypatch):
    tool, capture, _, _ = tool_env
    capture(File(name="a.txt", url="https://public.example/secret?private=yes"))

    async def fail(url):
        raise RuntimeError(url)

    monkeypatch.setattr(module, "download", fail)
    result = await tool.call({}, None, 1)
    assert json.loads(result)["complete"] is False
    assert "private" not in result and "https://" not in result


@pytest.mark.asyncio
async def test_complete_false_when_serialization_exceeds_host_limit(tool_env):
    tool, capture, _, _ = tool_env
    capture(File(name="a.txt", base64=base64.b64encode(b'"' * 16000).decode()))
    result = json.loads(await tool.call({}, None, 1))
    assert result["complete"] is False
    assert "text" not in result
