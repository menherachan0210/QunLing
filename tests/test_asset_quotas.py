"""Quota regressions with synthetic bytes; no live file deletion or QQ calls."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from qunling.assets import AssetError, AssetStore
from qunling.scope import Scope


OWNER = Scope("workspace", "bot", "group:1", "alice")


def make_legacy(store, scope):
    with store._connect() as db:
        db.execute("DELETE FROM asset_owners WHERE scope=?", (scope.key,))


def test_unassigned_legacy_assets_do_not_fill_every_conversation(tmp_path):
    store = AssetStore(tmp_path, conversation_bytes=6, max_bytes=10)
    old_scope = replace(OWNER, conversation_id="group:old")
    old = store.put(old_scope, "old.txt", b"12345")
    make_legacy(store, old_scope)

    # The previous LEFT JOIN charged these five bytes to this unrelated group,
    # preventing an otherwise valid import. They still consume the global cap.
    current = store.put(OWNER, "new.txt", b"1234")
    usage = store.usage(OWNER)
    assert usage["conversation"]["bytes"] == 4
    assert usage["storage"]["bytes"] == 9
    with pytest.raises(AssetError) as caught:
        store.put(OWNER, "next.txt", b"12")
    assert caught.value.code == "storage_full"
    assert store.read(old_scope, old["asset_ref"]) == b"12345"
    assert store.read(OWNER, current["asset_ref"]) == b"1234"


@pytest.mark.parametrize("operation", ["read", "list", "usage", "put"])
def test_trusted_access_recovers_legacy_owner_and_applies_group_cap(tmp_path, operation):
    store = AssetStore(tmp_path, conversation_bytes=6)
    bob = replace(OWNER, actor_id="bob")
    item = store.put(OWNER, "old.txt", b"12345")
    make_legacy(store, OWNER)
    if operation == "read":
        store.read(OWNER, item["asset_ref"])
    elif operation == "put":
        assert store.put(OWNER, "old.txt", b"12345") == item
    else:
        getattr(store, operation)(OWNER)
    assert store.put(bob, "one.txt", b"1")["size"] == 1
    with pytest.raises(AssetError) as caught:
        store.put(bob, "two.txt", b"2")
    assert caught.value.code == "conversation_quota"
    assert len(store.list(OWNER)["assets"]) == 1
    assert store.usage(bob)["conversation"]["bytes"] == 6


def test_failed_cross_scope_read_cannot_reassign_old_file(tmp_path):
    store = AssetStore(tmp_path)
    item = store.put(OWNER, "private.txt", b"private")
    make_legacy(store, OWNER)
    foreign = replace(OWNER, conversation_id="group:other")
    with pytest.raises(AssetError):
        store.read(foreign, item["asset_ref"])
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM asset_owners").fetchone()[0] == 0
    assert store.read(OWNER, item["asset_ref"]) == b"private"


@pytest.mark.parametrize("dimension", ["workspace_id", "bot_id", "conversation_id"])
def test_group_usage_isolates_all_conversation_dimensions(tmp_path, dimension):
    store = AssetStore(tmp_path, conversation_bytes=5)
    store.put(OWNER, "one.txt", b"12345")
    foreign = replace(OWNER, **{dimension: "other"})
    store.put(foreign, "two.txt", b"1234")
    assert store.usage(OWNER)["conversation"]["bytes"] == 5
    assert store.usage(foreign)["conversation"]["bytes"] == 4


def test_listing_shows_live_quota_but_keeps_request_visibility(tmp_path):
    now = [100.0]
    store = AssetStore(tmp_path, clock=lambda: now[0], retention=10,
                       member_bytes=20, conversation_bytes=30, max_bytes=40)
    store.put(OWNER, "expired.txt", b"123456")
    now[0] = 106
    old = store.put(OWNER, "old.txt", b"1234")
    context = {"created": 106, "request_key": "q"}
    now[0] = 111
    store.put(OWNER, "later.txt", b"12")
    store.put(replace(OWNER, actor_id="bob"), "bob.txt", b"123")
    listing = store.list(OWNER, context=context)
    assert [a["asset_ref"] for a in listing["assets"]] == [old["asset_ref"]]
    assert listing["usage"]["member"]["bytes"] == 6
    assert listing["usage"]["member"]["remaining_bytes"] == 14
    assert listing["usage"]["conversation"]["bytes"] == 9
    assert listing["usage"]["storage"]["remaining_bytes"] == 31
    now[0] = 122
    assert store.usage(OWNER)["storage"]["items"] == 0


@pytest.mark.parametrize("limits,code", [
    ({"member_assets": 1}, "member_quota"),
    ({"conversation_assets": 1}, "conversation_quota"),
    ({"max_assets": 1}, "storage_full"),
])
def test_full_count_quota_explains_cause_and_preserves_duplicate_reuse(tmp_path, limits, code):
    store = AssetStore(tmp_path, **limits)
    item = store.put(OWNER, "old.txt", b"1234")
    assert store.put(OWNER, "old.txt", b"1234") == item
    with pytest.raises(AssetError) as caught:
        store.put(OWNER, "new.txt", b"1234")
    assert caught.value.code == code
    assert "1/1 项" in str(caught.value)
    assert "本次需新增" in str(caught.value)
    assert "file_workspace list" in str(caught.value)
    assert store.read(OWNER, item["asset_ref"]) == b"1234"


def test_concurrent_members_cannot_overfill_shared_group(tmp_path):
    store = AssetStore(tmp_path, conversation_bytes=6)

    def put(actor):
        try:
            store.put(replace(OWNER, actor_id=actor), "input.txt", b"1234")
            return "ok"
        except AssetError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(put, ("alice", "bob"))) == ["conversation_quota", "ok"]
    assert store.usage(OWNER)["conversation"]["bytes"] == 4
