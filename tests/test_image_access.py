"""Image bans apply to trusted actors without changing chat/vision participation."""
import asyncio
from copy import deepcopy
import json

import pytest

from qunling.image_access import ImageAccessDenied, ImageAccessError, matching_rule, request_scope, require_current
from qunling.proactive.config import ConfigError, ConfigStore, defaults, validate
from qunling.scope import Scope


def rule(user="10001", group="", **changes):
    return {"user_id": user, "group_id": group, "note": "private admin note", "reply_mode": "persona", "reply_text": "", **changes}


def settings(*rows, **changes):
    return {**defaults(), "image_access": {"blocked_members": list(rows)}, **changes}


def scope(user="10001", conversation="group:30001", bot="bot-a"):
    return Scope("trusted-workspace", bot, conversation, user)


@pytest.mark.parametrize("conversation", ["group:30001", "group:30002", "person:10001"])
def test_global_rule_blocks_the_actor_across_groups_and_private_chats(conversation):
    config = validate(settings(rule()))
    assert matching_rule(config, scope(conversation=conversation))
    assert matching_rule(config, scope("10002", conversation)) is None


def test_group_rules_do_not_affect_other_groups_or_private_chat():
    config = validate(settings(rule(group="30001")))
    assert matching_rule(config, scope())
    assert matching_rule(config, scope(conversation="group:30002")) is None
    assert matching_rule(config, scope(conversation="person:10001")) is None


def test_group_reply_can_specialize_an_existing_global_ban():
    custom = rule(group="30001", reply_mode="text", reply_text="还想点菜呢？")
    config = validate(settings(rule(), custom))
    assert matching_rule(config, scope()) == custom
    assert matching_rule(config, scope(conversation="group:30002"))["reply_mode"] == "persona"


@pytest.mark.parametrize("change", [
    {"user_id": 123}, {"user_id": "00123"}, {"user_id": "@别人"}, {"user_id": "100\n01"},
    {"group_id": "30001;"}, {"group_id": None}, {"reply_mode": "allow"},
    {"reply_mode": "text", "reply_text": " "}, {"reply_text": "x" * 301},
    {"note": "x" * 81}, {"note": "\x00"}, {"reply_text": "\ud800"}, {"bot_id": "other"},
])
def test_invalid_blacklist_does_not_save(change, tmp_path):
    store = ConfigStore("bot-a", tmp_path)
    with pytest.raises(ConfigError):
        store.save(settings(rule(**change)), set())
    assert store.read() == defaults()


def test_duplicate_rules_rejected_and_scope_distinctions_remain_valid():
    with pytest.raises(ConfigError):
        validate(settings(rule(), rule()))
    assert len(validate(settings(rule(), rule(group="30001")))["image_access"]["blocked_members"]) == 2


def test_bans_survive_restart_and_older_console_saves_and_explicit_removal(tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    store = ConfigStore("bot-a")
    saved = store.save(settings(rule()), set())
    assert not saved["enabled"]  # Bans do not depend on free conversation being on.
    legacy = deepcopy(saved)
    legacy.pop("image_access")
    preserved = store.save(legacy, set())
    assert ConfigStore("bot-a").read()["image_access"] == saved["image_access"]
    with pytest.raises(ImageAccessDenied):
        with request_scope({"scope": scope().as_dict()}):
            pass
    with request_scope({"scope": scope(bot="bot-b").as_dict()}):
        require_current()
    with pytest.raises(ConfigError):
        store.save(saved, set())  # A stale admin form cannot remove newer rules.
    preserved["image_access"]["blocked_members"] = []
    store.save(preserved, set())
    with request_scope({"scope": scope().as_dict()}):
        require_current()


def test_block_response_stops_generation_and_does_not_expose_admin_notes():
    denied = ImageAccessDenied(rule())
    result = json.loads(denied.response())
    assert result["request_submitted"] is False
    assert result["reply"] == {"mode": "persona"}
    assert "当前群人格" in result["instruction"] and "嘲讽" in result["instruction"]
    assert not result["_qunling_task"]["can_continue"]
    assert result["_qunling_task"]["error_code"] == "permission"
    assert "private admin note" not in denied.response() and "10001" not in denied.response()
    exact = ImageAccessDenied(rule(reply_mode="text", reply_text="你这生图资格先排着吧。"))
    assert json.loads(exact.response())["reply"]["text"] == "你这生图资格先排着吧。"


def test_corrupt_permission_file_fails_closed_without_roasting_an_unknown_user(tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    store = ConfigStore("bot-a")
    store.save(settings(rule()), set())
    store.path.write_text('{broken', encoding="utf-8")
    with pytest.raises(ImageAccessError) as error:
        with request_scope({"scope": scope("10002").as_dict()}):
            pass
    assert not isinstance(error.value, ImageAccessDenied)
    require_current()  # Failed entry must still reset the context variable.


@pytest.mark.asyncio
async def test_parallel_actors_and_late_blacklist_changes_do_not_share_request_context(tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    store = ConfigStore("bot-a")
    entered = asyncio.Event()
    changed = asyncio.Event()

    async def denied_later():
        with request_scope({"scope": scope().as_dict()}):
            entered.set()
            await changed.wait()
            with pytest.raises(ImageAccessDenied):
                require_current()

    async def still_allowed():
        await entered.wait()
        with request_scope({"scope": scope("10002").as_dict()}):
            store.save(settings(rule()), set())
            changed.set()
            await asyncio.sleep(0)
            require_current()

    await asyncio.gather(denied_later(), still_allowed())
    require_current()
