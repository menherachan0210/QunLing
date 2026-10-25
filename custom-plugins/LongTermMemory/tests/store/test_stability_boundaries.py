import pytest

from store.memory_store import MemoryStore
from store.serialization import episode_scope
from test_memory_store_serialization import VectorStorageFixture


@pytest.mark.asyncio
async def test_similar_correction_never_replaces_another_person_or_fact():
    plugin = VectorStorageFixture()
    store = MemoryStore(plugin)
    old = await store.add_episode("kb", "model", "group-a", "Alice likes tea", sender_id="alice", fact_key="drink")
    await store.add_episode("kb", "model", "group-a", "Bob corrects: likes coffee", sender_id="bob", tags=["correction"])
    assert plugin.records[old["id"]]["metadata"]["status"] == "active"
    for speaker, fact, scope in [("bob", "drink", "group-a"), ("alice", "pet", "group-a"), ("alice", "drink", "group-b")]:
        with pytest.raises(ValueError, match="same speaker"):
            await store.add_episode("kb", "model", scope, "correction", sender_id=speaker,
                                    fact_key=fact, replaces_episode_id=old["id"])
    assert len(plugin.records) == 2


@pytest.mark.asyncio
async def test_unknown_l2_commit_fences_one_group_across_restart(monkeypatch):
    plugin = VectorStorageFixture()
    original = plugin.vector_upsert
    async def ambiguous(*args, **kwargs):
        await original(*args, **kwargs)  # May have committed before transport fails.
        raise RuntimeError("lost response")
    monkeypatch.setattr(plugin, "vector_upsert", ambiguous)
    store = MemoryStore(plugin)
    with pytest.raises(RuntimeError, match="lost response"):
        await store.add_episode("kb", "model", "group-a", "one")
    monkeypatch.setattr(plugin, "vector_upsert", original)
    store = MemoryStore(plugin)
    with pytest.raises(RuntimeError, match="fenced"):
        await store.add_episode("kb", "model", "group-a", "unsafe retry")
    await store.add_episode("kb", "model", "group-b", "unaffected group")
    await store.clear_fence(episode_scope("kb", "group-a"))
    await store.add_episode("kb", "model", "group-a", "operator reconciled")
    assert len(plugin.records) == 3


def test_legacy_profile_cannot_create_unbounded_prompt():
    profile = {"name": "n" * 20000, "traits": ["x" * 10000] * 20,
               "preferences": ["p" * 10000] * 10, "notes": "z" * 10000}
    assert len(MemoryStore.format_profile_prompt(profile)) <= 4000
    normalized = MemoryStore(VectorStorageFixture())._normalize_profile(profile)
    assert len(normalized["name"]) <= 160
    assert all(len(item) <= 500 for item in normalized["traits"] + normalized["preferences"])
