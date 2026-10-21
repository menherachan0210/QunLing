import base64
import json
from types import SimpleNamespace

import pytest
from langbot_plugin.api.entities.builtin.platform.message import File, MessageChain

from pkg import attachment_context, tools


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(
        attachment_context, "_CACHE", attachment_context.AttachmentCache()
    )
    variables, operations = {}, []

    class API:
        def __init__(self, **kwargs):
            pass

        async def get_query_vars(self):
            return variables

        async def get_bot_uuid(self):
            return "bot"

    class Manager:
        async def start(self, session, attachment, raw, model):
            operations.append(("start", session, attachment, raw, model))
            return {"status": "queued"}

        async def operate(self, session, action, query_api):
            operations.append((action, session))
            return {"status": "running"}

    plugin = SimpleNamespace(
        plugin_runtime_handler=object(),
        jobs=Manager(),
        get_config=lambda: {"model_uuid": "vision"},
        spawn_background=lambda: None,
    )
    monkeypatch.setattr(tools, "QueryBasedAPIProxy", API)

    def capture(file=None, sender="456"):
        source = SimpleNamespace(
            bot_uuid="bot",
            launcher_type="person",
            launcher_id="123",
            sender_id=sender,
            message_chain=MessageChain([file] if file else []),
        )
        variables[attachment_context.QUERY_CONTEXT_KEY] = (
            attachment_context.capture_context(plugin, source)
        )

    return SimpleNamespace(plugin=plugin), capture, operations


@pytest.mark.asyncio
async def test_start_binds_current_zip_and_no_model_supplied_scope(env):
    tool, capture, operations = env
    raw = b"synthetic raw zip for transport only"
    capture(
        File(name="fixture.zip", size=len(raw), base64=base64.b64encode(raw).decode())
    )
    result = json.loads(await tools.execute(tool, "start", {}, 1, None))
    assert result["status"] == "queued"
    _, session, attachment, received, model = operations[0]
    assert received == raw and model == "vision"
    assert session.sender_id == "456" and session.chat_id == "123"
    assert attachment.name == "fixture.zip"


@pytest.mark.asyncio
async def test_status_available_without_attachment_and_no_side_effect_on_bad_params(
    env,
):
    tool, capture, operations = env
    capture()
    assert (
        json.loads(await tools.execute(tool, "status", {}, 1, None))["status"]
        == "running"
    )
    assert operations[0][0] == "status"
    bad = await tools.execute(tool, "start", {"recipient": "other"}, 1, None)
    assert json.loads(bad)["status"] == "error" and len(operations) == 1


@pytest.mark.asyncio
async def test_incomplete_download_never_starts(env):
    tool, capture, operations = env
    capture(File(name="fixture.zip", size=20, base64=base64.b64encode(b"x").decode()))
    result = json.loads(await tools.execute(tool, "start", {}, 1, None))
    assert result["status"] == "error" and not operations


@pytest.mark.asyncio
async def test_start_and_password_only_resume_forward_password_without_echo(env):
    tool, capture, operations = env
    secret = "synthetic-private-value"
    raw = b"synthetic encrypted transport placeholder"
    capture(File(name="fixture.zip", size=len(raw), base64=base64.b64encode(raw).decode()))

    async def begin(session, attachment, received, model, *, password):
        assert password == secret and received == raw
        operations.append(("password-start", session.sender_id))
        return {"status": "awaiting_password"}

    async def resume(session, action, query_api, *, password):
        assert password == secret and action == "resume"
        operations.append(("password-resume", session.sender_id))
        return {"status": "queued"}

    tool.plugin.jobs.start = begin
    tool.plugin.jobs.operate = resume
    result = await tools.execute(tool, "start", {"password": secret}, 1, None)
    assert json.loads(result)["status"] == "awaiting_password" and secret not in result
    capture()  # The user's subsequent password-only request has no file attached.
    result = await tools.execute(tool, "resume", {"password": secret}, 2, None)
    assert json.loads(result)["status"] == "queued" and secret not in result
    assert operations == [("password-start", "456"), ("password-resume", "456")]


@pytest.mark.asyncio
@pytest.mark.parametrize("action, params", [
    ("status", {"password": "unneeded"}),
    ("start", {"password": True}),
    ("resume", {"password": "x\ny"}),
    ("resume", {"password": "value", "sender_id": "other"}),
])
async def test_bad_password_arguments_cannot_reach_task_manager(env, action, params):
    tool, capture, operations = env
    capture()
    result = json.loads(await tools.execute(tool, action, params, 1, None))
    assert result["status"] == "error" and not operations
