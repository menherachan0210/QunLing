from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qunling.memory_relevance import cosine, relevant_candidates


@pytest.mark.asyncio
async def test_best_ranked_unrelated_memory_is_rejected_before_boosts():
    plugin = SimpleNamespace(invoke_embedding=AsyncMock(return_value=[[0, 1], [0.9, 0.1]]))
    rows = [{"id": "wrong", "distance": 0, "metadata": {"content": "不相关", "importance": "5"}},
            {"id": "right", "distance": 0.9, "metadata": {"content": "相关", "importance": "1"}}]
    results = await relevant_candidates(plugin, "emb", [1, 0], rows)
    assert [item["id"] for item in results] == ["right"]
    assert results[0]["semantic_similarity"] > 0.99


@pytest.mark.parametrize("vector", [[0, 0], [float("nan"), 0], [float("inf"), 1], [1], []])
def test_invalid_vectors_never_become_relevant(vector):
    assert cosine([1, 0], vector) is None


@pytest.mark.asyncio
async def test_batch_failure_is_not_reported_as_relevant():
    plugin = SimpleNamespace(invoke_embedding=AsyncMock(return_value=[]))
    with pytest.raises(ValueError):
        await relevant_candidates(plugin, "emb", [1, 0], [{"metadata": {"content": "原文"}}])
