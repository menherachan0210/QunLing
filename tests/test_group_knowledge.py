"""Scoped public lookup summaries across plugin and conversational processes."""
from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from qunling import group_knowledge as knowledge


WORKSPACE = knowledge.workspace_id("instance", "workspace")


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    clock = NS(now=time.time() - 30)
    monkeypatch.setattr(knowledge, "time", NS(time=lambda: clock.now))
    return clock, tmp_path


def publish(**changes):
    value = dict(workspace_id=WORKSPACE, bot_id="bot", group_id="30000", question="看看小明的头像",
                 source="avatar_analysis", subject="小明的头像", text="头像中有一只戴眼镜的白猫。")
    return knowledge.publish(**(value | changes))


def recall(**changes):
    value = dict(workspace_id=WORKSPACE, bot_id="bot", group_id="30000", question="小明的头像是什么样")
    return knowledge.recall(**(value | changes))


def test_plugin_and_host_workspace_key_format_match(cache):
    from qunling.integration import workspace_key

    plugin = NS(get_installation_binding=lambda: NS(instance_uuid="instance", workspace_uuid="workspace"))
    assert WORKSPACE == workspace_key(plugin)
    assert knowledge.workspace_id(None, "workspace") == ""


def test_public_evidence_is_readable_in_new_connection_and_contains_no_execution_scope(cache):
    assert publish()
    rows = recall()
    assert rows == [{"source": "avatar_analysis", "subject": "小明的头像", "text": "头像中有一只戴眼镜的白猫。",
                     "timestamp": int(cache[0].now)}]
    assert "question" not in rows[0] and "bot_id" not in rows[0]


@pytest.mark.parametrize("scope", [
    {"workspace_id": knowledge.workspace_id("other-instance", "workspace")},
    {"workspace_id": knowledge.workspace_id("instance", "other-workspace")},
    {"bot_id": "other-bot"}, {"group_id": "90000"},
])
def test_other_instance_workspace_bot_and_group_cannot_read(cache, scope):
    assert publish()
    assert recall(**scope) == []


@pytest.mark.parametrize("scope", [{"workspace_id": ""}, {"workspace_id": "workspace"},
                                   {"bot_id": "bad\nbot"}, {"group_id": True},
                                   {"group_id": "-1"}, {"group_id": "../30000"}])
def test_invalid_identity_is_rejected_before_storage(cache, scope):
    assert not publish(**scope)
    assert recall(**scope) == []
    assert not (cache[1] / "group-knowledge.sqlite3").exists()


def test_expiry_and_frozen_request_cutoff(cache):
    clock, _ = cache
    assert publish()
    assert recall(before=clock.now - .1) == []
    assert recall(before=clock.now)
    clock.now += knowledge.TTL_SECONDS
    assert recall() == []
    assert recall(before=clock.now - 300) == []
    assert not recall(before=float("inf"))


def test_refresh_replaces_old_same_subject_result(cache):
    clock, path = cache
    assert publish()
    clock.now += 1
    assert publish(text="头像现在是一只黑猫。")
    assert len(recall()) == 1 and "黑猫" in recall()[0]["text"]
    with sqlite3.connect(path / "group-knowledge.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1


def test_unsupported_private_or_file_content_source_is_rejected(cache):
    for source in ("file_body", "private_message", "task_status", "model_memory", "", []):
        assert not publish(source=source)
    assert recall() == []


def test_relevance_ignores_independent_topic_but_supports_short_followup(cache):
    assert publish()
    assert recall(question="周末看什么电影", nearby_text="看看小明的头像") == []
    assert recall(question="那它是什么颜色", nearby_text="我们刚在看小明的头像")
    assert recall(question="那它是什么颜色", nearby_text="我们刚在讨论周末电影") == []
    assert recall(question="那小红的头像呢", nearby_text="我们刚在看小明的头像") == []
    assert recall(question="这个呢") == []


def test_urls_handles_and_credentials_are_removed_before_storage_and_on_read(cache):
    clock, path = cache
    raw = ('小明的头像 https://cdn.example/avatar?token=secret-value '
           'asset_0123456789abcdef0123456789abcdef file_ref="private-file" '
           'file_id: raw-qq-id folder_ref: secret-folder storage_path=/private/path '
           'api_key="important key with spaces" 密码是 test-pass Bearer opaque-token '
           'member_0123456789abcdef0123456789abcdef folder_0123456789abcdef01234567 '
           'gfile_abcdef0123456789abcdef01 avatar_1 '
           'data:image/png;base64,AAABBB')
    assert publish(text=raw, subject="小明的头像", question="小明的头像 password=hidden")
    with sqlite3.connect(path / "group-knowledge.sqlite3") as db:
        stored = repr(db.execute("SELECT question,subject,text FROM evidence").fetchall())
        for value in ("cdn.example", "secret-value", "private-file", "raw-qq-id", "secret-folder",
                      "/private/path", "important key", "test-pass", "opaque-token", "AAABBB", "hidden",
                      "0123456789abcdef0123456789abcdef", "0123456789abcdef01234567", "abcdef0123456789abcdef01", "avatar_1"):
            assert value not in stored
        db.execute("UPDATE evidence SET text=?", ("小明头像 https://untrusted.example asset_0123456789abcdef0123456789abcdef",))
    result = json.dumps(recall(), ensure_ascii=False)
    assert "untrusted.example" not in result and "0123456789abcdef0123456789abcdef" not in result


def test_retention_and_retrieval_are_bounded(cache, monkeypatch):
    clock, path = cache
    monkeypatch.setattr(knowledge, "MAX_TOTAL", 20)
    for index in range(30):
        clock.now += .1
        assert publish(subject=f"头像成员{index}", question="群友头像资料", text="群友头像资料" + "猫" * 2000)
    with sqlite3.connect(path / "group-knowledge.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == knowledge.MAX_PER_GROUP
        assert db.execute("SELECT MAX(LENGTH(text)) FROM evidence").fetchone()[0] == 1200
    for group in range(90000, 90040):
        clock.now += .1
        assert publish(group_id=str(group))
    with sqlite3.connect(path / "group-knowledge.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 20
    for index in range(12):
        clock.now += .1
        assert publish(subject=f"头像成员{index}", question="群友头像资料", text="群友头像资料" + "猫" * 2000)
    result = recall(question="群友头像资料")
    assert 1 <= len(result) <= knowledge.MAX_RESULTS
    assert sum(len(row["subject"]) + len(row["text"]) for row in result) <= knowledge.MAX_RESULT_CHARS


def test_storage_unavailable_fails_closed_and_read_does_not_create_db(tmp_path, monkeypatch):
    monkeypatch.delenv("QUNLING_DATA_DIR", raising=False)
    assert not publish() and recall() == []
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    assert recall() == []
    assert not (tmp_path / "group-knowledge.sqlite3").exists()


# Import the real HostBridge fixture: only external model/KB boundaries are fake.
from test_proactive_host import bridge, event, query_for  # noqa: E402,F401


@pytest.mark.asyncio
async def test_free_reply_and_explicit_request_share_relevant_public_summary_only_after_decision(bridge, monkeypatch):
    from qunling import group_memory
    from qunling.proactive import host
    from qunling.proactive.engine import ConversationContext, Decision, Message

    assert publish()
    monkeypatch.setattr(group_memory, "recall_group_memory", AsyncMock(return_value={"records": []}))
    target = Message("bot", "30000", "m1", "20002", "小明的头像是什么样", time.time())
    context = ConversationContext("bot", "30000", "active", "", (target,), (), "", None, None)
    bridge._complete = AsyncMock(return_value='{"action":"reply","target_message_id":"m1"}')
    original = host._shared_knowledge
    seen = []
    def capture(*args, **kwargs):
        seen.append((args, kwargs))
        return original(*args, **kwargs)
    monkeypatch.setattr(host, "_shared_knowledge", capture)
    await bridge.judge(context)
    assert seen == []
    bridge._complete.return_value = "是戴眼镜的白猫。"
    await bridge.generate(context, Decision("reply", "m1"))
    payload = bridge._complete.await_args.args[1]
    assert len(seen) == 1 and payload["shared_knowledge"][0]["subject"] == "小明的头像"
    assert payload["runtime"]["tools_available"] is False
    request = event("小明的头像是什么样", explicit=True, sender="20003")
    bridge.observe(request)
    query = query_for(bridge, request)
    host.prepare_prompt(query)
    summaries = [m for m in query.prompt.messages if isinstance(m.content, str) and m.content.startswith(host.KNOWLEDGE_MARKER)]
    assert len(summaries) == 1 and "戴眼镜的白猫" in summaries[0].content
    assert summaries[0].role == "user"
    host.prepare_prompt(query)
    assert sum(m.content.startswith(host.KNOWLEDGE_MARKER) for m in query.prompt.messages) == 1
    await bridge.engine.close()


@pytest.mark.asyncio
async def test_explicit_request_cannot_gain_newer_lookup_or_foreign_workspace_summary(bridge):
    from qunling.proactive import host

    request = event("小明的头像是什么样", explicit=True)
    bridge.observe(request)
    assert publish()
    query = query_for(bridge, request)
    host.prepare_prompt(query)
    assert not any(m.content.startswith(host.KNOWLEDGE_MARKER) for m in query.prompt.messages)
    assert publish(workspace_id=knowledge.workspace_id("instance", "other-workspace"))
    assert host._shared_knowledge(bridge.bot, "30000", "小明的头像", before=request.timestamp) == []
    bridge.bot.enable = False
    assert host._shared_knowledge(bridge.bot, "30000", "小明的头像") == []
    await bridge.engine.close()
