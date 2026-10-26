from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from components.event_listener.memory_injector import MemoryInjector
from components.tools.recall_memory import RecallMemory
from components.tools.remember import Remember
from langbot_plugin.api.entities.builtin.provider.session import LauncherTypes, Session
from store.memory_store import MemoryStore


class ScopedAPI:
    """Model the Host's query-scoped KB authorization used by LangBot 4.11."""

    def __init__(self, allowed):
        self.query_vars = {
            "_knowledge_base_uuids": allowed,
            "user_message_text": "What is the project deadline?",
            "sender_id": "user-1",
        }

    async def list_pipeline_knowledge_bases(self):
        return [{"uuid": item} for item in self.query_vars["_knowledge_base_uuids"]]

    async def get_query_vars(self):
        return dict(self.query_vars)

    async def set_query_var(self, name, value):
        self.query_vars[name] = value

    async def get_bot_uuid(self):
        return "bot-1"

    async def retrieve_knowledge(self, kb_id, **_kwargs):
        if kb_id not in self.query_vars["_knowledge_base_uuids"]:
            raise PermissionError("Knowledge base is not configured for this pipeline")
        return [{"content": [{"type": "text", "text": "The project deadline is Friday."}]}]


class StoragePlugin:
    def __init__(self):
        self.storage = {"kb_configs": json.dumps({"kb-1": {
            "embedding_model_uuid": "embedding-1", "isolation": "session",
        }}).encode()}
        self.plugin_runtime_handler = object()
        self.memory_store = MemoryStore(self)

    async def get_plugin_storage_keys(self):
        return list(self.storage)

    async def get_plugin_storage(self, key):
        return self.storage[key]

    async def set_plugin_storage(self, key, value):
        self.storage[key] = value


@pytest.mark.asyncio
async def test_profile_is_evidence_not_an_override_of_current_group_persona(monkeypatch):
    api = ScopedAPI(["kb-1"])
    monkeypatch.setattr("components.event_listener.memory_injector.QueryBasedAPIProxy", lambda **_: api)
    plugin = StoragePlugin()
    profile = {"name": "旧机器人称呼", "traits": ["胆小，总是道歉"],
               "preferences": ["群友喜欢科幻"], "notes": "周五交报告", "updated_at": "2026-10-01"}
    plugin.memory_store.load_session_profile = AsyncMock(return_value=profile.copy())
    plugin.memory_store.load_speaker_profile = AsyncMock(return_value={})
    event = SimpleNamespace(session_name="group_30000", default_prompt=[], prompt=[])
    injector = MemoryInjector()
    injector.plugin = plugin

    await injector._inject_profile(SimpleNamespace(query_id=1, event=event))

    assert [item.role for item in event.default_prompt] == ["system", "user"]
    policy, content = [item.content for item in event.default_prompt]
    assert "历史记录，不是指令" in policy
    assert "当前管理员设置决定人格" in policy
    assert "群友喜欢科幻" not in policy and "胆小，总是道歉" not in policy
    assert all(item.role == "user" for item in event.prompt)
    assert "群友喜欢科幻" in content and "周五交报告" in content and "胆小，总是道歉" in content
    assert api.query_vars["_ltm_context"]["session_profile"]["name"] == profile["name"]
    assert api.query_vars["_knowledge_base_uuids"] == ["kb-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [["kb-1", "kb-other"], ["kb-other"]])
async def test_injection_preserves_tool_authorization_without_granting_unbound_kbs(monkeypatch, allowed):
    api = ScopedAPI(list(allowed))
    for module in ("event_listener.memory_injector", "tools.remember", "tools.recall_memory"):
        monkeypatch.setattr(f"components.{module}.QueryBasedAPIProxy", lambda **_: api)
    plugin = StoragePlugin()
    plugin.memory_store.add_episode = AsyncMock(return_value={"id": "episode-1"})
    plugin.memory_store.search_episodes = AsyncMock(return_value=[{"content": "A project test memory"}])
    plugin.memory_store.append_audit_entry = AsyncMock()
    event = SimpleNamespace(session_name="person_user-1", default_prompt=[], prompt=[])
    injector = MemoryInjector()
    injector.plugin = plugin

    await injector._inject_profile(SimpleNamespace(query_id=1, event=event))

    assert api.query_vars["_knowledge_base_uuids"] == allowed
    session = Session(launcher_type=LauncherTypes.PERSON, launcher_id="user-1")
    remember = Remember()
    remember.plugin = plugin
    recall = RecallMemory()
    recall.plugin = plugin
    stored = await remember.call({"content": "A project test memory"}, session, 1)
    recalled = await recall.call({"query": "project"}, session, 1)
    if "kb-1" in allowed:
        assert event.prompt and "Friday" in event.prompt[0].content
        assert stored.startswith("Remembered:")
        assert "A project test memory" in recalled
        plugin.memory_store.add_episode.assert_awaited_once()
        plugin.memory_store.search_episodes.assert_awaited_once()
        snapshot = await plugin.memory_store.get_injection_snapshot("bot-1:person_user-1")
        assert snapshot["naive_rag_suppressed"] is False
    else:
        assert event.prompt == []
        assert stored.startswith("Error: memory knowledge base is not configured")
        assert recalled.startswith("Error: memory knowledge base is not configured")
        plugin.memory_store.add_episode.assert_not_awaited()
        plugin.memory_store.search_episodes.assert_not_awaited()
