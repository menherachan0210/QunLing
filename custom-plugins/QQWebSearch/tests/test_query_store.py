from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from pkg import context as state
from pkg.context import QueryContext, QueryStore
from pkg.errors import WebError


class QueryAPI:
    def __init__(self, variables=None):
        self.variables = {} if variables is None else variables
        self.writes = 0

    async def get_query_vars(self):
        return self.variables

    async def set_query_var(self, key, value):
        self.writes += 1
        await asyncio.sleep(0)
        self.variables[key] = value


@pytest.mark.asyncio
async def test_same_query_reuses_context_and_parallel_creation_writes_one_token():
    store, api = QueryStore(), QueryAPI()
    first, second = await asyncio.gather(store.get(api, 1, "a"), store.get(api, 1, "a"))
    assert first is second
    assert api.writes == 1 and len(store.entries) == 1
    assert first.query_id == 1 and first.query_uuid == "a"


@pytest.mark.asyncio
async def test_new_query_has_independent_images_counts_and_delivery():
    store = QueryStore()
    first = await store.get(QueryAPI(), 1, "a")
    second = await store.get(QueryAPI(), 2, "b")
    first.claim("search", 1)
    first.delivery = "sent"
    ref = store.remember(first, {"data": b"image"})
    assert ref in first.images and ref not in second.images
    assert second.counts == {} and second.delivery is None


@pytest.mark.asyncio
@pytest.mark.parametrize("query_id,query_uuid", [(2, "a"), (1, "b"), (1, None)])
async def test_copied_query_token_cannot_access_another_query(query_id, query_uuid):
    store, api = QueryStore(), QueryAPI()
    await store.get(api, 1, "a")
    with pytest.raises(WebError, match="不属于"):
        await store.get(QueryAPI(dict(api.variables)), query_id, query_uuid)
    assert len(store.entries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["unknown", 1, ["forged"], {"key": "forged"}])
async def test_forged_nonempty_tokens_cannot_create_a_fresh_context(token):
    store, api = QueryStore(), QueryAPI({state.KEY: token})
    with pytest.raises(WebError, match="过期"):
        await store.get(api, 1, "a")
    assert api.writes == 0 and store.entries == {}


@pytest.mark.asyncio
async def test_missing_query_variables_fail_closed():
    store, api = QueryStore(), QueryAPI()
    api.get_query_vars = AsyncMock(return_value=None)
    with pytest.raises(WebError, match="当前会话"):
        await store.get(api, 1, "a")
    assert store.entries == {} and api.writes == 0


@pytest.mark.asyncio
async def test_expired_token_cannot_reset_tool_allowance(monkeypatch):
    store, api = QueryStore(), QueryAPI()
    context = await store.get(api, 1, "a")
    context.claim("images", 1)
    monkeypatch.setattr(
        state.time, "monotonic", lambda: context.created + state.TTL + 1
    )
    with pytest.raises(WebError, match="过期"):
        await store.get(api, 1, "a")
    assert api.writes == 1 and store.entries == {}


@pytest.mark.asyncio
async def test_at_capacity_existing_unexpired_context_is_still_accessible(monkeypatch):
    monkeypatch.setattr(state, "MAX_QUERIES", 2)
    store, first_api = QueryStore(), QueryAPI()
    first = await store.get(first_api, 1, "a")
    await store.get(QueryAPI(), 2, "b")
    assert await store.get(first_api, 1, "a") is first
    assert len(store.entries) == 2


@pytest.mark.asyncio
async def test_new_query_evicts_oldest_when_at_capacity(monkeypatch):
    monkeypatch.setattr(state, "MAX_QUERIES", 2)
    store, first_api = QueryStore(), QueryAPI()
    first = await store.get(first_api, 1, "a")
    second = await store.get(QueryAPI(), 2, "b")
    third = await store.get(QueryAPI(), 3, "c")
    assert len(store.entries) == 2
    assert first not in store.entries.values()
    assert second in store.entries.values() and third in store.entries.values()
    with pytest.raises(WebError, match="过期"):
        await store.get(first_api, 1, "a")


def test_action_limits_are_independent_and_failed_claim_does_not_reset():
    context = QueryContext(1, "a")
    for _ in range(3):
        context.claim("search", 3)
    with pytest.raises(WebError, match="上限"):
        context.claim("search", 3)
    context.claim("page", 4)
    assert context.counts == {"search": 3, "page": 1}


@pytest.mark.asyncio
async def test_memory_budget_is_global_not_per_query(monkeypatch):
    monkeypatch.setattr(state, "MAX_BYTES", 5)
    store = QueryStore()
    first = await store.get(QueryAPI(), 1, "a")
    second = await store.get(QueryAPI(), 2, "b")
    first_ref = store.remember(first, {"data": b"123"})
    second_ref = store.remember(second, {"data": b"45"})
    assert first_ref != second_ref
    with pytest.raises(WebError, match="缓存"):
        store.remember(second, {"data": b"6"})
    assert len(first.images) == len(second.images) == 1


@pytest.mark.asyncio
async def test_clear_invalidates_cached_query_tokens():
    store, api = QueryStore(), QueryAPI()
    await store.get(api, 1, "a")
    store.clear()
    with pytest.raises(WebError, match="过期"):
        await store.get(api, 1, "a")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner",
    [
        None,
        ("other-instance", "workspace", "installation", 1),
        ("instance", "other-workspace", "installation", 1),
        ("instance", "workspace", "other-installation", 1),
        ("instance", "workspace", "installation", 2),
    ],
)
async def test_query_token_cannot_cross_installation_or_revision(owner):
    store, api = QueryStore(), QueryAPI()
    await store.get(api, 1, "a", owner=("instance", "workspace", "installation", 1))
    with pytest.raises(WebError, match="不属于"):
        await store.get(api, 1, "a", owner=owner)
    assert api.writes == 1


@pytest.mark.asyncio
async def test_matching_installation_can_continue_existing_query():
    store, api = QueryStore(), QueryAPI()
    owner = ("instance", "workspace", "installation", 1)
    first = await store.get(api, 1, "a", owner=owner)
    assert await store.get(api, 1, "a", owner=owner) is first


@pytest.mark.asyncio
async def test_active_query_is_not_pruned_until_operation_finishes(monkeypatch):
    store, api = QueryStore(), QueryAPI()
    context = await store.get(api, 1, "a")
    context.active = 1
    monkeypatch.setattr(
        state.time, "monotonic", lambda: context.created + state.TTL + 1
    )
    store.prune()
    assert context in store.entries.values()
    context.active = 0
    store.prune()
    assert store.entries == {}


@pytest.mark.asyncio
async def test_at_capacity_new_query_never_evicts_active_work(monkeypatch):
    monkeypatch.setattr(state, "MAX_QUERIES", 1)
    store = QueryStore()
    context = await store.get(QueryAPI(), 1, "a")
    context.active = 1
    another = QueryAPI()
    with pytest.raises(WebError, match="较多"):
        await store.get(another, 2, "b")
    assert context in store.entries.values()
    assert another.writes == 0
