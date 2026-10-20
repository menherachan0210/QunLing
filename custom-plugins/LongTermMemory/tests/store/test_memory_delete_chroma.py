from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from langbot.pkg.rag.service.runtime import RAGRuntimeService
from langbot.pkg.vector.vdbs.chroma import ChromaVectorDatabase
from store.memory_store import MemoryStore


class ChromaHostPlugin:
    """Use the real Host delete contract and an isolated real Chroma database."""

    def __init__(self, tmp_path):
        self.database = ChromaVectorDatabase(SimpleNamespace(logger=MagicMock()), str(tmp_path / "chroma"))
        self.storage = {"kb_configs": json.dumps({"memory-kb": {
            "embedding_model_uuid": "local-test", "isolation": "session",
        }}).encode()}
        self.upsert_ids = []

        async def delete_files(*, knowledge_base_uuid, file_ids, **_kwargs):
            for file_id in file_ids:
                await self.database.delete_by_file_id(knowledge_base_uuid, file_id)

        async def delete_filter(*, knowledge_base_uuid, filter, **_kwargs):
            return await self.database.delete_by_filter(knowledge_base_uuid, filter)

        self.runtime = RAGRuntimeService(SimpleNamespace(vector_db_mgr=SimpleNamespace(
            delete_by_file_id=delete_files, delete_by_filter=delete_filter,
        )))
        self.runtime._resolve_knowledge_base_uuid = AsyncMock(return_value="memory-kb")

    async def get_plugin_storage_keys(self):
        return list(self.storage)

    async def get_plugin_storage(self, key):
        return self.storage[key]

    async def set_plugin_storage(self, key, value):
        self.storage[key] = value

    async def invoke_embedding(self, _model, texts):
        return [[1.0, 0.0] for _ in texts]

    async def vector_upsert(self, collection_id, vectors, ids, metadata=None, documents=None):
        self.upsert_ids.extend(ids)
        await self.database.add_embeddings(collection_id, ids, vectors, metadata, documents)

    async def vector_list(self, collection_id, filters=None, limit=20, offset=0):
        items, total = await self.database.list_by_filter(collection_id, filters, limit, offset)
        return {"items": items, "total": total}

    async def vector_delete(self, collection_id, file_ids=None, filters=None):
        return await self.runtime.vector_delete(None, collection_id, file_ids, filters)

    async def vector_search(self, collection_id, query_vector, top_k=5, filters=None, **_kwargs):
        result = await self.database.search(collection_id, query_vector, top_k, filter=filters)
        return [{"id": item_id, "metadata": result["metadatas"][0][index]}
                for index, item_id in enumerate(result["ids"][0])]


@pytest.mark.asyncio
async def test_episode_deletion_uses_real_host_semantics_and_preserves_other_scopes(tmp_path):
    plugin = ChromaHostPlugin(tmp_path)
    store = MemoryStore(plugin)
    first = await store.add_episode("memory-kb", "local-test", "scope-a", "Test memory A")
    second = await store.add_episode("memory-kb", "local-test", "scope-b", "Test memory B")
    await plugin.vector_upsert("memory-kb", [[0.0, 1.0]], ["foreign-collision"],
                               [{"file_id": first["id"], "user_key": "scope-b", "content": "Keep this record"}],
                               ["Keep this record"])

    assert await store.delete_episode_by_id("memory-kb", first["id"], "scope-b") == 0
    assert await store.get_episode_by_id("memory-kb", first["id"], "scope-a") is not None
    assert await store.delete_episode_by_id("memory-kb", first["id"], "scope-a") == 1
    assert await store.get_episode_by_id("memory-kb", first["id"], "scope-a") is None
    assert await store.get_episode_by_id("memory-kb", second["id"], "scope-b") is not None
    assert await store.get_episode_by_id("memory-kb", "foreign-collision", "scope-b") is not None
    assert await store.delete_episode_by_id("memory-kb", first["id"], "scope-a") == 0


@pytest.mark.asyncio
async def test_legacy_episode_only_repairs_and_deletes_the_requested_scoped_record(tmp_path):
    plugin = ChromaHostPlugin(tmp_path)
    await plugin.vector_upsert("memory-kb", [[1.0, 0.0], [0.0, 1.0]], ["legacy-a", "legacy-b"],
                               [{"content": "Legacy A", "user_key": "scope-a"},
                                {"content": "Legacy B", "user_key": "scope-b"}],
                               ["Legacy A", "Legacy B"])
    plugin.upsert_ids.clear()
    store = MemoryStore(plugin)

    assert await store.delete_episode_by_id("memory-kb", "legacy-a", "scope-b") == 0
    assert plugin.upsert_ids == []
    assert await store.delete_episode_by_id("memory-kb", "legacy-a", "scope-a") == 1
    assert plugin.upsert_ids == ["legacy-a"]
    assert await store.get_episode_by_id("memory-kb", "legacy-a", "scope-a") is None
    other = await store.get_episode_by_id("memory-kb", "legacy-b", "scope-b")
    assert other["content"] == "Legacy B"
    assert "file_id" not in other["metadata"]


@pytest.mark.asyncio
async def test_health_probe_cleans_all_its_records_without_touching_existing_memory(tmp_path):
    plugin = ChromaHostPlugin(tmp_path)
    store = MemoryStore(plugin)
    existing = await store.add_episode("memory-kb", "local-test", "scope-a", "Protected test memory")

    result = await store.run_metadata_filter_probe("memory-kb", "local-test")

    assert result["status"] == "OK"
    records = await plugin.vector_list("memory-kb")
    assert [item["id"] for item in records["items"]] == [existing["id"]]


@pytest.mark.asyncio
async def test_delete_does_not_trust_a_false_success_count(tmp_path):
    plugin = ChromaHostPlugin(tmp_path)
    store = MemoryStore(plugin)
    episode = await store.add_episode("memory-kb", "local-test", "scope-a", "Test memory")
    plugin.runtime.ap.vector_db_mgr.delete_by_filter = AsyncMock(return_value=1)

    with pytest.raises(RuntimeError, match="not confirmed"):
        await store.delete_episode_by_id("memory-kb", episode["id"], "scope-a")

    assert await store.get_episode_by_id("memory-kb", episode["id"], "scope-a") is not None
