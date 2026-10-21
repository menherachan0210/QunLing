"""Opt-in group recall reads the same L1 storage as explicit message injection."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from components.knowledge_engine.memory_engine import LongTermMemoryEngine
from components.knowledge_engine import memory_engine
from langbot_plugin.api.entities.builtin.rag.context import RetrievalContext
from store.memory_store import MemoryStore


class ReadOnlyPlugin:
    def __init__(self):
        self.memory_store = MemoryStore(self)
        self.read_keys = []
        self.storage = {
            "pp:bot:group_30000:20000": {"name": "小南", "preferences": ["喜欢漫画校对"], "updated_at": "2026-10-21T01:00:00Z"},
            "ps:bot:group_30000": {"notes": "这个群周六一起讨论翻译"},
            "pp:bot:group_30000:20001": {"notes": "其他成员未选中的画像"},
            "pp:bot:group_90000:20000": {"notes": "同一个人其他群的秘密"},
            "ps:bot:person_20000": {"notes": "私聊秘密"},
            "ps:other:group_30000": {"notes": "其他机器人秘密"},
        }

    async def get_plugin_storage(self, key):
        self.read_keys.append(key)
        if key not in self.storage:
            raise KeyError(key)
        return json.dumps(self.storage[key], ensure_ascii=False).encode()

    async def set_plugin_storage(self, *args):
        raise AssertionError("Recall must never write")

    async def invoke_embedding(self, embedding, texts):
        return [[1.0] for _ in texts]

    async def vector_search(self, **kwargs):
        return [{"id": f"episode-{i}", "distance": 0.1, "metadata": {
            "user_key": "bot:group_30000", "sender_id": "20001", "content": f"最近的翻译讨论{i}",
            "status": "active", "timestamp": "2026-10-21T02:00:00Z",
        }} for i in range(3)]


def context(**overrides):
    return RetrievalContext(query="周六讨论", knowledge_base_id="memory",
        creation_settings={"embedding_model_uuid": "embedding", "isolation": "session"},
        retrieval_settings={"session_name": "group_30000", "sender_id": "20000", "bot_uuid": "bot",
                            "include_profiles": True, "top_k": 3, **overrides})


@pytest.fixture
def engine():
    value = LongTermMemoryEngine()
    value.plugin = ReadOnlyPlugin()
    return value


@pytest.mark.asyncio
async def test_group_and_current_member_profiles_share_three_record_budget_with_episode(engine):
    before = deepcopy(engine.plugin.storage)
    response = await engine.retrieve(context())
    assert len(response.results) == response.total_found == 3
    speaker, group, episode = response.results
    assert speaker.metadata["memory_kind"] == "speaker_profile"
    assert speaker.metadata["sender_id"] == "20000"
    assert speaker.metadata["sender_name"] == "小南"
    assert "喜欢漫画校对" in speaker.metadata["content"]
    assert group.metadata["memory_kind"] == "group_profile" and group.metadata["sender_id"] == ""
    assert "周六" in group.metadata["content"]
    assert episode.id.startswith("episode-") and episode.metadata["sender_id"] == "20001"
    assert all(r.metadata["user_key"] == "bot:group_30000" for r in response.results)
    assert engine.plugin.read_keys == ["pp:bot:group_30000:20000", "ps:bot:group_30000"]
    assert engine.plugin.storage == before
    assert "秘密" not in response.model_dump_json() and "未选中的画像" not in response.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, "true", None])
async def test_default_explicit_request_behavior_does_not_read_or_add_profiles(engine, flag):
    request = context(include_profiles=flag)
    if flag is None:
        request.retrieval_settings.pop("include_profiles")
    response = await engine.retrieve(request)
    assert len(response.results) == 3 and all(r.id.startswith("episode-") for r in response.results)
    assert engine.plugin.read_keys == []


@pytest.mark.asyncio
@pytest.mark.parametrize("settings", [
    {"session_name": "person_20000"}, {"session_name": "group_"}, {"session_name": "group_../30000"},
    {"session_name": "group_0"}, {"bot_uuid": ""},
])
async def test_profile_extension_refuses_non_group_or_unproven_bot_scope(engine, settings):
    response = await engine.retrieve(context(**settings))
    assert all("memory_kind" not in r.metadata for r in response.results)
    assert engine.plugin.read_keys == []


@pytest.mark.asyncio
async def test_global_isolation_does_not_borrow_group_profiles(engine):
    request = context()
    request.creation_settings["isolation"] = "global"
    await engine.retrieve(request)
    assert engine.plugin.read_keys == []


@pytest.mark.asyncio
async def test_empty_profiles_leave_entire_budget_for_episodes(engine):
    engine.plugin.storage = {}
    response = await engine.retrieve(context())
    assert len(response.results) == 3 and all(r.id.startswith("episode-") for r in response.results)


@pytest.mark.asyncio
async def test_other_member_only_gets_own_same_group_profile(engine):
    response = await engine.retrieve(context(sender_id="20001"))
    assert engine.plugin.read_keys == ["pp:bot:group_30000:20001", "ps:bot:group_30000"]
    assert response.results[0].metadata["sender_id"] == "20001"
    assert "小南" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_corrupt_profile_read_keeps_episode_results_and_other_valid_profile(engine):
    engine.plugin.storage["pp:bot:group_30000:20000"] = "invalid"
    response = await engine.retrieve(context())
    assert any(r.id.startswith("episode-") for r in response.results)
    assert any(r.metadata.get("memory_kind") == "group_profile" for r in response.results)


@pytest.mark.asyncio
async def test_embedding_error_keeps_existing_profiles_without_changing_default_requests(engine):
    async def fail_embedding(*args):
        raise RuntimeError("internal api_key=must-not-log")
    engine.plugin.invoke_embedding = fail_embedding
    response = await engine.retrieve(context())
    assert [r.metadata["memory_kind"] for r in response.results] == ["speaker_profile", "group_profile"]
    with pytest.raises(RuntimeError):
        await engine.retrieve(context(include_profiles=False))


@pytest.mark.asyncio
async def test_cold_embedding_is_cancelled_before_overall_host_deadline_and_keeps_profiles(engine, monkeypatch):
    monkeypatch.setattr(memory_engine, "GROUP_EPISODE_TIMEOUT_SECONDS", 0.02)
    cancelled = asyncio.Event()
    async def slow_embedding(*args):
        assert engine.plugin.read_keys == ["pp:bot:group_30000:20000", "ps:bot:group_30000"]
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    engine.plugin.invoke_embedding = slow_embedding
    async with asyncio.timeout(0.2):
        response = await engine.retrieve(context())
    assert cancelled.is_set()
    assert len(response.results) == 2
    assert all(r.metadata["memory_kind"].endswith("profile") for r in response.results)


@pytest.mark.asyncio
async def test_slow_profile_does_not_block_other_profile_and_episode(engine, monkeypatch):
    monkeypatch.setattr(memory_engine, "PROFILE_READ_TIMEOUT_SECONDS", 0.02)
    original = engine.plugin.get_plugin_storage
    async def storage(key):
        if key.startswith("pp:"):
            await asyncio.Event().wait()
        return await original(key)
    engine.plugin.get_plugin_storage = storage
    async with asyncio.timeout(0.2):
        response = await engine.retrieve(context())
    assert response.results[0].metadata["memory_kind"] == "group_profile"
    assert response.results[1].id.startswith("episode-")


@pytest.mark.asyncio
async def test_without_embedding_configuration_existing_l1_data_still_available(engine):
    request = context()
    request.creation_settings.pop("embedding_model_uuid")
    response = await engine.retrieve(request)
    assert len(response.results) == 2
    assert all(r.metadata["memory_kind"].endswith("profile") for r in response.results)
