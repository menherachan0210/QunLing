"""Exercise model-selected QQ operations against the pinned API contract; no QQ I/O."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import jsonschema
import pytest

from qunling.qq_actions import QQActions, QQActionError, sanitize
from qunling.qq_capabilities import OPS, continuation_allowed
from qunling.qq_operation_store import OperationStore

CONTRACTS = json.loads((Path(__file__).parent / "fixtures/napcat-4.18.37-qq-requests.json").read_text(encoding="utf-8"))


@pytest.fixture
def service(tmp_path):
    calls = []
    roles = {"101": "admin", "202": "member", "999": "owner"}
    messages = {"17": {"message_id": 17, "message_type": "group", "group_id": "300",
        "sender": {"user_id": "202"}, "message": [{"type": "text", "data": {"text": "hello"}}]},
        "18": {"message_id": 18, "message_type": "group", "group_id": "99900", "sender": {"user_id": "202"}}}
    async def call(action, **arguments):
        jsonschema.validate(arguments, CONTRACTS[action])
        calls.append((action, deepcopy(arguments)))
        if action == "get_group_member_info":
            return {"group_id": arguments["group_id"], "user_id": arguments["user_id"], "role": roles[arguments["user_id"]]}
        if action == "get_msg":
            return deepcopy(messages[arguments["message_id"]])
        if action in {"get_group_root_files", "get_group_files_by_folder"}:
            return {"files": [{"file_id": "f1", "file_name": "a.txt"}], "folders": [{"folder_id": "d1", "folder_name": "docs"}]}
        if action == "get_ai_characters":
            return [{"type": "voice", "characters": [{"character_id": "voice1", "character_name": "角色"}]}]
        if action == "_get_group_notice":
            return [{"notice_id": "n1", "text": "notice"}]
        if action == "create_group_file_folder":
            return {"folder_id": "d2"}
        return {"message_id": 20} if action.startswith("send_") else {}
    query = NS(adapter=NS(bot=NS(call_action=call), bot_account_id="999"), bot_uuid="bot",
        sender_id="101", launcher_type="group", launcher_id="300", query_uuid="query1", query_id=1,
        message_event=NS(message_id="17"), message_chain=[], variables={})
    config = {"groups": {}, "qq_actions": {"enabled": True, "owner_ids": ["101"], "transcription_model": ""}}
    value = QQActions(query, config, OperationStore(tmp_path / "qq.sqlite"), instance="i", workspace="w")
    value.calls, value.roles, value.messages = calls, roles, messages
    value.record_sent = lambda *args: None
    return value


CASES = {
    "group_info": {}, "members": {}, "honors": {}, "notices": {}, "essence": {}, "muted_members": {},
    "at_all_remaining": {}, "file_storage": {}, "files": {}, "message": {}, "events": {}, "requests": {},
    "friends": {}, "bot_status": {}, "poke": {"user_id": "202"}, "react": {"emoji_id": "76"},
    "like": {"user_id": "202", "times": 2}, "face": {"kind": "face", "face_id": 76},
    "say": {"text": "test", "user_id": "202", "message_id": "17"}, "at_all": {"text": "notice"},
    "forward": {"message_ids": ["17"]}, "recall": {"message_id": "17"},
    "mute": {"user_id": "202", "duration": 60}, "kick": {"user_id": "202"}, "whole_mute": {"enable": True},
    "group_name": {"group_name": "new"}, "member_card": {"user_id": "202", "card": "name"},
    "member_title": {"user_id": "202", "special_title": "title"}, "administrator": {"user_id": "202", "enable": True},
    "leave": {}, "publish_notice": {"content": "notice"}, "delete_notice": {"notice_id": "n1"},
    "set_essence": {"message_id": "17", "enable": True}, "create_folder": {"folder_name": "new"},
    "delete_folder": {"folder_id": "d1"}, "delete_file": {"file_id": "f1"},
    "rename_file": {"file_id": "f1", "new_name": "new.txt"}, "move_file": {"file_id": "f1", "target_parent_directory": "d1"},
    "voices": {}, "speak": {"character": "voice1", "text": "test"}, "media": {},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,args", CASES.items())
async def test_operations_obey_pinned_wire_contract(service, operation, args):
    if operation == "leave":
        service.roles["999"] = "admin"
    if operation in {"delete_folder", "delete_file", "rename_file", "move_file"}:
        await service.execute("files", {})
    if operation == "delete_notice":
        await service.execute("notices", {})
    if operation == "speak":
        await service.execute("voices", {})
    result = await service.execute(operation, args)
    assert result["status"] in {"ok", "confirmed"}, result
    for endpoint, arguments in service.calls:
        if "group_id" in arguments:
            assert arguments["group_id"] == "300"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["mute", "kick", "at_all", "publish_notice", "delete_file", "administrator", "leave", "friends"])
async def test_member_cannot_borrow_bot_permissions(service, operation):
    service.owner = False
    service.roles["101"] = "member"
    with pytest.raises(QQActionError):
        await service.execute(operation, CASES[operation])
    assert all(c[0] == "get_group_member_info" for c in service.calls)


@pytest.mark.asyncio
async def test_group_scope_quote_and_permissions_cannot_be_overridden(service):
    for args in ({"user_id": "202", "group_id": "999"}, {"user_id": "202", "actor": "999"}):
        with pytest.raises(ValueError):
            await service.execute("poke", args)
    for op, args in [("message", {"message_id": "18"}), ("react", {"message_id": "18", "emoji_id": "1"}),
                     ("forward", {"message_ids": ["17", "18"]}), ("recall", {"message_id": "18"})]:
        with pytest.raises(QQActionError, match="当前会话"):
            await service.execute(op, args)
    assert not any(c[0].startswith("send_") for c in service.calls)
    service.permissions["interaction"] = False
    with pytest.raises(QQActionError, match="未开放"):
        await service.execute("poke", {"user_id": "202"})


@pytest.mark.asyncio
async def test_file_notice_and_voice_ids_must_be_read_in_this_query(service):
    for op in ("move_file", "delete_notice", "speak"):
        with pytest.raises(QQActionError):
            await service.execute(op, CASES[op])
    await service.execute("files", {})
    assert (await service.execute("create_folder", {"folder_name": "next"}))["status"] == "confirmed"
    assert (await service.execute("move_file", {"file_id": "f1", "target_parent_directory": "d2"}))["status"] == "confirmed"
    assert (await service.execute("rename_file", CASES["rename_file"]))["status"] == "confirmed"
    assert service.calls[-1][1]["current_parent_directory"] == "d2"


@pytest.mark.asyncio
async def test_concurrent_and_restarted_repeats_are_not_sent_twice(service):
    result = await asyncio.gather(*(service.execute("poke", {"user_id": "202"}) for _ in range(4)))
    assert sum(c[0] == "group_poke" for c in service.calls) == 1
    assert sum(bool(r.get("already_attempted")) for r in result) == 3
    service.store = OperationStore(service.store.path)
    assert (await service.execute("poke", {"user_id": "202"}))["already_attempted"]


@pytest.mark.asyncio
async def test_unknown_receipt_stays_unknown_and_is_not_retried(service):
    call = service.bot.call_action
    async def fail(action, **args):
        if action == "group_poke":
            raise TimeoutError()
        return await call(action, **args)
    service.bot.call_action = AsyncMock(side_effect=fail)
    first = await service.execute("poke", {"user_id": "202"})
    second = await service.execute("poke", {"user_id": "202"})
    assert first["status"] == second["status"] == "unconfirmed" and second["already_attempted"]
    assert sum(call.args[0] == "group_poke" for call in service.bot.call_action.call_args_list) == 1


@pytest.mark.asyncio
async def test_requests_are_real_flags_and_once_across_queries(service):
    service.store.event(service.scope_id, "group:300", {"type": "group", "request": True, "sub_type": "add", "flag": "secret"})
    refs = await service.execute("requests", {})
    assert "secret" not in json.dumps(refs)
    ref = refs["events"][0]["request_ref"]
    await service.execute("respond_request", {"request_ref": ref, "approve": True})
    service.request = "query2"
    repeated = await service.execute("respond_request", {"request_ref": ref, "approve": False})
    assert repeated["already_attempted"] and repeated["status"] == "confirmed"
    assert sum(c[0] == "set_group_add_request" for c in service.calls) == 1
    assert (await service.execute("requests", {}))["events"][0]["operation_status"] == "confirmed"


@pytest.mark.asyncio
async def test_private_actions_stay_on_current_peer(service):
    service.kind, service.chat, service.chat_id = "person", "person:101", "101"
    await service.execute("poke", {"user_id": "101"})
    assert service.calls[-1] == ("friend_poke", {"user_id": "101"})
    with pytest.raises(QQActionError):
        await service.execute("poke", {"user_id": "202"})
    with pytest.raises(QQActionError):
        await service.execute("whole_mute", {"enable": True})


def test_freechat_allows_semantic_interaction_but_not_management_or_file_send():
    for op in ("poke", "react", "face", "media", "transcribe", "video_frames", "analyze_images"):
        assert continuation_allowed({"operation": op})
    for op in ("kick", "mute", "upload_file", "speak", "send_media", "leave"):
        assert not continuation_allowed({"operation": op})
    assert not continuation_allowed({"operation": []})
    assert not continuation_allowed({"operation": "poke", "group_id": "other"})


def test_result_strips_platform_secrets_and_local_paths():
    result = sanitize({"flag": "secret", "content": {"url": "https://signed", "file": "/srv/file", "text": "hello"}})
    assert result == {"content": {"text": "hello"}}


@pytest.mark.asyncio
async def test_owner_can_demote_admin_but_cannot_dissolve_group(service):
    service.roles["202"] = "admin"
    result = await service.execute("administrator", {"user_id": "202", "enable": False})
    assert result["status"] == "confirmed"
    with pytest.raises(QQActionError, match="转让"):
        await service.execute("leave", {})
    assert not any(c[0] == "set_group_leave" for c in service.calls)


@pytest.mark.asyncio
async def test_optional_defaults_do_not_bypass_deduplication(service):
    await service.execute("like", {"user_id": "202"})
    result = await service.execute("like", {"user_id": "202", "times": 1})
    assert result["already_attempted"]
    assert sum(c[0] == "send_like" for c in service.calls) == 1


@pytest.mark.asyncio
async def test_tool_capacity_error_is_reported_without_private_exception_text():
    from qunling.qq_actions import handle
    from qunling.resource_gate import ResourceBusy
    handler = NS(_resolve_query=AsyncMock(side_effect=ResourceBusy('private signed reference')))
    identity = NS(plugin_author='local', plugin_name='QQGroupContext')
    result = await handle(handler, {'params': {}}, None, identity)
    assert result['status'] == 'error' and result['code'] == 'capacity'
    assert '服务器暂时繁忙' in result['error'] and 'private signed reference' not in str(result)
