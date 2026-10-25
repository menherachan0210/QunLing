import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from qunling.knowledge.config import ConfigError, defaults, public_scope, requires_live, validate
from qunling.knowledge.store import Store
from qunling.scope import Scope

OWNER = Scope("workspace", "bot", "group:30000", "20000")
SOURCE = {"group_id": "30000", "url": "https://public.example/article", "interval": 60, "ttl": 120}
PAGE = {"url": SOURCE["url"], "title": "Python 文档", "text": "Python coroutine 异步协程工作原理", "quality": "readable"}


def test_evidence_scope_expiry_and_read_state(tmp_path):
    now = [1000]
    store = Store(tmp_path / "web.db", clock=lambda: now[0])
    assert store.put(OWNER, "page:"+PAGE["url"], "page", {**PAGE, "page_ref": "not-reusable", "_read_status": "read"})
    assert store.recall(OWNER, "异步协程")
    for scope in (replace(OWNER, workspace_id="other"), replace(OWNER, bot_id="other"),
                  replace(OWNER, conversation_id="group:40000"), replace(OWNER, actor_id="other")):
        assert not store.get(scope, "page:"+PAGE["url"])
        assert not store.recall(scope, "异步协程")
    result = store.get(OWNER, "page:"+PAGE["url"])
    assert "page_ref" not in result and "_read_status" not in result
    assert result["cache"]["fetched_at"] == 1000
    now[0] += 3601
    assert not store.get(OWNER, "page:"+PAGE["url"])
    assert not store.recall(OWNER, "异步协程")


def test_subscription_lease_budget_revision_fence_and_group_visibility(tmp_path):
    now = [1000]
    store = Store(tmp_path / "web.db", clock=lambda: now[0])
    config = store.save(OWNER, {**defaults(), "daily_budget": 1, "sources": [SOURCE]}, {"30000"})
    token = store.claim(OWNER, SOURCE, config["revision"])
    assert token and store.claim(OWNER, SOURCE, config["revision"]) is None
    assert not store.complete(OWNER, SOURCE, config["revision"], "wrong", PAGE)
    assert store.complete(OWNER, SOURCE, config["revision"], token, PAGE)
    assert store.recall(replace(OWNER, actor_id="other"), "异步协程")
    now[0] += 61
    assert store.claim(OWNER, SOURCE, config["revision"]) is None
    now[0] += 86400
    old = store.claim(OWNER, SOURCE, config["revision"])
    assert old
    store.save(OWNER, {**config, "enabled": False}, {"30000"})
    assert not store.complete(OWNER, SOURCE, config["revision"], old, PAGE)
    assert not store.recall(OWNER, "异步协程")
    with pytest.raises(ConfigError):
        store.save(OWNER, config, {"30000"})
    # A readable response rejected by the storage limit is not a successful collection.
    now[0] += 86400
    config = store.save(OWNER, {**store.settings(OWNER), "enabled": True}, {"30000"})
    token = store.claim(OWNER, SOURCE, config["revision"])
    assert not store.complete(OWNER, SOURCE, config["revision"], token, {**PAGE, "text": "x" * (768 * 1024)})
    status = store.status(OWNER)
    assert status["counters"]["collected"] == 1 and status["counters"]["failed"] == 1
    assert next(iter(status["sources"].values()))["status"] == "content_limit"


def test_validation_and_freshness_do_not_hide_live_requests(tmp_path):
    for text in ("核实这个说法", "今天的新闻", "现在价格", "search latest release", "联网查一下"):
        assert requires_live(text)
    assert not requires_live("Python 异步协程原理")
    for url in ("file:///etc/passwd", "http://127.0.0.1/", "http://user:password@example.com", None, [], {}, 123):
        with pytest.raises(ConfigError):
            validate({**defaults(), "sources": [{**SOURCE, "url": url}]})
    with pytest.raises(ConfigError):
        Store(tmp_path / "web.db").save(OWNER, {**defaults(), "sources": [SOURCE]}, {"40000"})


@pytest.mark.asyncio
async def test_collector_does_not_publish_after_config_or_group_revocation(tmp_path, monkeypatch):
    from qunling.knowledge import host
    from qunling.proactive import host as proactive
    store = Store(tmp_path / "web.db")
    initial = store.save(OWNER, {**defaults(), "sources": [SOURCE]}, {"30000"})
    bot = NS(adapter=NS(get_group_list=AsyncMock(return_value=[NS(id="30000")])))
    monkeypatch.setattr(proactive, "_bridge", lambda *a, **k: NS(_active=AsyncMock()))
    monkeypatch.setattr(host, "scope_for", lambda *a, **k: OWNER)
    async def revoked(url):
        store.save(OWNER, {**initial, "enabled": False}, {"30000"})
        return PAGE
    await host.cycle(bot, store, revoked)
    assert not store.recall(OWNER, "异步协程")
    current = store.settings(OWNER)
    store.save(OWNER, {**current, "enabled": True}, {"30000"})
    async def left(url):
        bot.adapter.get_group_list.return_value = []
        return PAGE
    await host.cycle(bot, store, left)
    assert not store.recall(OWNER, "异步协程")
