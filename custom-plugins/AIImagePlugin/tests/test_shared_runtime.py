# Modified by qq-ai-bot contributors on 2026-10-20; local changes, see NOTICE.
"""Two installations must not share one OpenAI credential or endpoint.

Pre-fix failure this test pins: ``AIImagePlugin.initialize()`` cached a single
``AsyncOpenAI`` built from whichever config the worker started with, and
``Draw._execute`` called ``self.plugin.openai_client``. With that code the second
installation's ``!draw`` was sent with installation A's API key to installation
A's base URL, and ``assert not hasattr(plugin, "openai_client")`` fails.

Both installations are driven through the *same* plugin object and the *same*
``Draw`` component object under the real SDK ``bind_invocation`` /
``InstallationBinding`` task-local scope; only the per-binding config differs.
``sys.modules["openai"]`` is replaced with a recorder so no network or real SDK is
involved (the ``openai`` package is not installed for these local runs).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langbot_plugin.api.proxies.invocation import bind_invocation
from langbot_plugin.entities.io.context import InstallationBinding

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from components.commands.draw import Draw  # noqa: E402
from main import AIImagePlugin  # noqa: E402

CONFIG_A = {
    "openai_api_key": "key-a",
    "api_base_url": "https://a.example/v1",
    "model_name": "model-a",
    "image_size": "768x1280",
}
CONFIG_B = {
    "openai_api_key": "key-b",
    "api_base_url": "https://b.example/v1",
    "model_name": "model-b",
    "image_size": "1280x768",
}


def _binding(installation: str) -> InstallationBinding:
    return InstallationBinding(
        instance_uuid="instance-1",
        workspace_uuid=f"workspace-{installation}",
        installation_uuid=installation,
        runtime_revision=1,
        artifact_digest="a" * 64,
    )


def _ctx(params: list[str], plugin, *, sender="10001") -> SimpleNamespace:
    from qunling.integration import CONTEXT_KEY, descriptor
    trusted = descriptor(plugin, SimpleNamespace(bot_uuid="bot", launcher_type="group",
                         launcher_id="30001", sender_id=sender), "command-query")
    return SimpleNamespace(
        params=params, reply=AsyncMock(), query_uuid="command-query", get_bot_uuid=AsyncMock(return_value="bot"),
        get_query_vars=AsyncMock(return_value={CONTEXT_KEY: trusted})
    )


@pytest.fixture
def fake_openai(monkeypatch):
    """Record every AsyncOpenAI construction, generate call, and close."""

    created: list[dict] = []

    class _Images:
        def __init__(self, entry: dict) -> None:
            self._entry = entry

        async def generate(self, **kwargs):
            self._entry["generate"] = kwargs
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        url=f"https://img.test/{self._entry['api_key']}.png"
                    )
                ]
            )

    class _AsyncOpenAI:
        def __init__(
            self, *, api_key: str, base_url: str, timeout: float, max_retries: int, http_client
        ) -> None:
            assert timeout == 300
            assert max_retries == 0
            entry = {"api_key": api_key, "base_url": base_url, "closed": False}
            created.append(entry)
            self.entry = entry
            self.http_client = http_client
            self.images = _Images(entry)

        async def close(self) -> None:
            self.entry["closed"] = True
            await self.http_client.aclose()

    module = types.ModuleType("openai")
    module.AsyncOpenAI = _AsyncOpenAI
    monkeypatch.setitem(sys.modules, "openai", module)
    response_types = types.ModuleType("openai.types")
    response_types.ImagesResponse = SimpleNamespace
    monkeypatch.setitem(sys.modules, "openai.types", response_types)
    return created


@pytest.fixture(autouse=True)
def isolated_image_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))


@pytest.mark.asyncio
async def test_each_installation_uses_its_own_key_base_url_model_and_size(
    fake_openai,
) -> None:
    handler = SimpleNamespace()
    plugin = AIImagePlugin()
    plugin.plugin_runtime_handler = handler
    draw = Draw()
    draw.plugin = plugin

    with bind_invocation(handler, config=CONFIG_A, binding=_binding("installation-a")):
        ctx_a = _ctx(["a", "cat"], plugin)
        results_a = [item async for item in draw._execute(ctx_a)]
    with bind_invocation(handler, config=CONFIG_B, binding=_binding("installation-b")):
        ctx_b = _ctx(["b", "dog"], plugin)
        results_b = [item async for item in draw._execute(ctx_b)]

    assert [(entry["api_key"], entry["base_url"]) for entry in fake_openai] == [
        ("key-a", "https://a.example/v1"),
        ("key-b", "https://b.example/v1"),
    ]
    assert fake_openai[0]["generate"] == {
        "model": "model-a",
        "prompt": "a cat",
        "size": "768x1280",
        "n": 1,
        "quality": "auto",
        "response_format": "b64_json",
    }
    assert fake_openai[1]["generate"] == {
        "model": "model-b",
        "prompt": "b dog",
        "size": "1280x768",
        "n": 1,
        "quality": "auto",
        "response_format": "b64_json",
    }
    assert ctx_a.reply.await_args.args[0][0].url == "https://img.test/key-a.png"
    assert results_a[-1].text == "图片已生成并发送。"
    assert ctx_b.reply.await_args.args[0][0].url == "https://img.test/key-b.png"
    assert results_b[-1].text == "图片已生成并发送。"
    assert [entry["closed"] for entry in fake_openai] == [True, True]

    # Nothing on the shared object retains a single tenant's credential.
    assert not hasattr(plugin, "openai_client")
    assert "key-a" not in repr(vars(plugin))
    assert "key-b" not in repr(vars(plugin))
    assert "a.example" not in repr(vars(plugin))
    assert "b.example" not in repr(vars(plugin))


@pytest.mark.asyncio
async def test_missing_key_reports_without_building_a_client(fake_openai) -> None:
    handler = SimpleNamespace()
    plugin = AIImagePlugin()
    plugin.plugin_runtime_handler = handler
    draw = Draw()
    draw.plugin = plugin

    config = {"openai_api_key": "", "api_base_url": "https://a.example/v1"}
    with bind_invocation(handler, config=config, binding=_binding("installation-a")):
        results = [item async for item in draw._execute(_ctx(["a", "cat"], plugin))]

    assert fake_openai == []
    assert "API Key" in results[-1].text
    assert results[-1].image_url is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,text", [("persona", ""), ("text", "生图权限还没排到你呢。")])
async def test_draw_command_cannot_bypass_member_bans(fake_openai, mode, text):
    from qunling.proactive.config import ConfigStore, defaults
    ConfigStore("bot").save({**defaults(), "image_access": {"blocked_members": [{
        "user_id": "10001", "group_id": "30001", "note": "", "reply_mode": mode, "reply_text": text}]}}, set())
    handler = SimpleNamespace()
    plugin = AIImagePlugin()
    plugin.plugin_runtime_handler = handler
    draw = Draw()
    draw.plugin = plugin
    with bind_invocation(handler, config=CONFIG_A, binding=_binding("installation-a")):
        ctx = _ctx(["a", "cat"], plugin)
        results = [item async for item in draw._execute(ctx)]
    assert fake_openai == []
    ctx.reply.assert_not_awaited()
    assert len(results) == 1
    assert results[0].text == (text or "生图资格都没了，还搁这儿点菜呢。")


@pytest.mark.asyncio
async def test_draw_command_without_trusted_sender_never_submits(fake_openai):
    handler = SimpleNamespace()
    plugin = AIImagePlugin()
    plugin.plugin_runtime_handler = handler
    draw = Draw()
    draw.plugin = plugin
    with bind_invocation(handler, config=CONFIG_A, binding=_binding("installation-a")):
        ctx = _ctx(["a", "cat"], plugin)
        ctx.get_query_vars.return_value = {}
        results = [item async for item in draw._execute(ctx)]
    assert fake_openai == []
    ctx.reply.assert_not_awaited()
    assert "上下文" in results[-1].text


def test_dedicated_config_without_an_invocation_is_still_used(fake_openai) -> None:
    # Dedicated workers attach the legacy config and call the same code path.
    plugin = AIImagePlugin()
    plugin.plugin_runtime_handler = SimpleNamespace()
    plugin.config = {
        "openai_api_key": "dedicated-key",
        "api_base_url": "https://d.example/v1",
    }

    client = plugin.create_client()

    assert client.entry["api_key"] == "dedicated-key"
    assert client.entry["base_url"] == "https://d.example/v1"
