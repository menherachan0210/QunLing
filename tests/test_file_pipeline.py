"""Shared pipeline acceptance; synthetic bytes only, no network, model or QQ."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock
import time

import pytest

pytestmark = [pytest.mark.sdk, pytest.mark.usefixtures('available_file_resources')]
from PIL import Image

from qunling import capture, inbox as inbox_module, integration, transfers
from qunling.assets import AssetError, AssetStore, image_name
from qunling.inbox import Inbox
from qunling.network import Fetched
from qunling.processing import run_operation
from qunling.scope import Scope


def scope():
    return Scope("workspace-a", "bot-a", "group:100", "sender-a")


def test_asset_listing_keeps_question_boundary_but_accepts_explicit_grants(tmp_path):
    now = [100.0]
    store = AssetStore(tmp_path, clock=lambda: now[0])
    owner = scope()
    old = store.put(owner, "old.txt", b"old")
    context = {"created": 100.0, "request_key": "question"}
    now[0] = 101
    later = store.put(owner, "later.txt", b"later unrelated upload")
    result = store.put(owner, "result.txt", b"this request output", origin="generated")
    store.grant(owner, "question", result["asset_ref"])
    visible = {item["asset_ref"] for item in store.list(owner, context=context)["assets"]}
    assert visible == {old["asset_ref"], result["asset_ref"]}
    with pytest.raises(AssetError):
        store.require_visible(owner, later["asset_ref"], context)
    store.require_visible(owner, result["asset_ref"], context)
    other_query = {"created": 100.0, "request_key": "other-question"}
    with pytest.raises(AssetError):
        store.require_visible(owner, result["asset_ref"], other_query)


def test_same_bytes_different_filename_share_delivery_receipt(tmp_path):
    store, owner = AssetStore(tmp_path), scope()
    first = store.put(owner, "first.txt", b"same content")
    renamed = store.put(owner, "renamed.txt", b"same content")
    assert store.reserve_delivery(owner, "request", first["asset_ref"])
    store.mark_delivery(owner, "request", first["asset_ref"], "unconfirmed")
    assert not store.reserve_delivery(owner, "request", renamed["asset_ref"])
    assert store.delivery(owner, "request", renamed["asset_ref"]) == "unconfirmed"


def plugin(installation="reader"):
    binding = SimpleNamespace(
        instance_uuid="instance-a", workspace_uuid="workspace-a",
        installation_uuid=installation, placement_generation=1,
    )
    return SimpleNamespace(get_installation_binding=lambda: binding)


def source(*chain, **changes):
    return SimpleNamespace(**{
        "bot_uuid": "bot-a", "launcher_type": "group", "launcher_id": "100",
        "sender_id": "sender-a", "message_chain": list(chain), **changes,
    })


def file_component(name="notes.txt", data=b"synthetic text"):
    return SimpleNamespace(
        type="File", name=name, size=len(data), id="", url="",
        base64=base64.b64encode(data).decode(),
    )


def picture(color, image_format):
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(stream, format=image_format)
    return stream.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("inline", [True, False])
async def test_separate_qq_image_messages_keep_distinct_content_names(monkeypatch, tmp_path, inline):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    host = plugin()
    owner = integration.source_scope(host, source())
    store = AssetStore.from_env()
    pictures = [picture("red", "PNG"), picture("blue", "JPEG")]
    fetch = AsyncMock(side_effect=[
        Fetched("https://qq.example/one", pictures[0], "image/png"),
        Fetched("https://qq.example/two", pictures[1], "image/jpeg"),
    ])
    monkeypatch.setattr(transfers, "fetch_public_file", fetch)
    imported = []
    for index, raw in enumerate(pictures):
        component = SimpleNamespace(
            type="Image", url=f"https://qq.example/{index}",
            base64=base64.b64encode(raw).decode() if inline else "", image_id="",
        )
        context = capture.capture_message(host, source(component), f"request-{index}")
        item = await transfers.import_attachment(
            host, store, owner, context["inbox_refs"][0], before=context["created"],
            current_refs=context["inbox_refs"],
        )
        assert item["name"] == image_name(raw)
        assert store.read(owner, item["asset_ref"]) == raw
        imported.append(item)
    assert imported[0]["name"] != imported[1]["name"]
    assert imported[0]["asset_ref"] != imported[1]["asset_ref"]
    assert imported[0]["name"].endswith(".png") and imported[1]["name"].endswith(".jpg")
    assert fetch.await_count == (0 if inline else 2)
    # An explicitly named QQ File remains named by the source, even for image bytes.
    file_context = capture.capture_message(host, source(file_component("page-01.png", pictures[0])), "file-q")
    named = await transfers.import_attachment(
        host, store, owner, file_context["inbox_refs"][0], before=file_context["created"],
        current_refs=file_context["inbox_refs"],
    )
    assert named["name"] == "page-01.png"


def api(context, *, bot="bot-a", query="request-a"):
    return SimpleNamespace(
        query_uuid=query, get_bot_uuid=AsyncMock(return_value=bot),
        get_query_vars=AsyncMock(return_value={integration.CONTEXT_KEY: context}),
        reply=AsyncMock(),
    )


@pytest.mark.parametrize("field,value", [
    ("workspace_id", "foreign"), ("bot_id", "bot-b"),
    ("conversation_id", "group:101"), ("actor_id", "sender-b"),
])
def test_persistent_assets_isolate_each_scope_dimension(tmp_path, field, value):
    store, owner = AssetStore(tmp_path), scope()
    item = store.put(owner, "notes.txt", b"private bytes", metadata={"url": "https://private-source/"})
    other = replace(owner, **{field: value})
    with pytest.raises(AssetError) as caught:
        store.read(other, item["asset_ref"])
    assert caught.value.code == "not_found"
    assert not store.list(other)["assets"]
    assert "url" not in store.get(owner, item["asset_ref"])
    assert store.read(owner, item["asset_ref"]) == b"private bytes"


def test_asset_expiry_invalidates_reads_and_next_insert_prunes_expired_blob(tmp_path):
    now = [1000.0]
    store = AssetStore(tmp_path, clock=lambda: now[0], retention=10)
    item = store.put(scope(), "old.txt", b"old")
    now[0] = 1011
    with pytest.raises(AssetError) as caught:
        store.read(scope(), item["asset_ref"])
    assert caught.value.code == "not_found" and not store.list(scope())["assets"]
    store.put(scope(), "new.txt", b"new")
    assert not (store.blobs / item["sha256"]).exists()


def test_equal_timestamp_asset_pages_do_not_skip_or_duplicate_rows(tmp_path):
    store = AssetStore(tmp_path, clock=lambda: 1000)
    expected = {
        store.put(scope(), f"{i}.txt", str(i).encode())["asset_ref"] for i in range(7)
    }
    cursor, found = None, []
    while True:
        result = store.list(scope(), cursor=cursor, limit=2)
        found.extend(row["asset_ref"] for row in result["assets"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert len(found) == len(set(found)) == 7 and set(found) == expected


@pytest.mark.asyncio
async def test_trusted_scope_is_shared_by_plugins_in_same_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    reader, web = plugin("reader"), plugin("web")
    context = capture.capture_message(reader, source(file_component()), "request-a")
    trusted = await integration.query_scope(web, api(context))
    assert trusted == integration.source_scope(reader, source())
    entry = Inbox(AssetStore.from_env()).get(trusted, context["inbox_refs"][0])
    reopened = AssetStore.from_env()
    assert reopened.read(trusted, entry["asset_ref"]) == b"synthetic text"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workspace", "bot", "query", "expired", "future"])
async def test_context_rejects_mismatched_or_stale_host_identity(monkeypatch, kind):
    host = plugin()
    context = integration.descriptor(host, source(), "request-a")
    target = api(context)
    if kind == "workspace":
        host.get_installation_binding().workspace_uuid = "other"
    elif kind == "bot":
        target.get_bot_uuid.return_value = "bot-b"
    elif kind == "query":
        target.query_uuid = "request-b"
    elif kind == "expired":
        context["created"] -= integration.CONTEXT_TTL + 1
    else:
        context["created"] += 10
    with pytest.raises(AssetError) as caught:
        await integration.query_scope(host, target)
    assert caught.value.code == "scope_unavailable"


def test_same_sender_later_inbox_items_do_not_change_existing_request(monkeypatch, tmp_path):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    now = [1000.0]
    timer = SimpleNamespace(time=lambda: now[0])
    monkeypatch.setattr(integration, "time", timer)
    monkeypatch.setattr(inbox_module, "time", timer)
    monkeypatch.setattr(capture, "time", timer)
    host = plugin()
    first = capture.capture_message(host, source(file_component("first.txt", b"first")), "q1")
    now[0] += 1
    second = capture.capture_message(host, source(file_component("later.txt", b"later")), "q2")
    owner = integration.source_scope(host, source())
    inbox = Inbox(AssetStore.from_env())
    visible = inbox.list(owner, before=first["created"])
    assert [row["incoming_ref"] for row in visible] == first["inbox_refs"]
    assert not set(first["inbox_refs"]) & set(second["inbox_refs"])
    with pytest.raises(AssetError):
        inbox.get(owner, second["inbox_refs"][0], before=first["created"])


@pytest.mark.asyncio
async def test_await_window_excludes_same_actor_later_file_but_allows_current_refs(monkeypatch, tmp_path):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    now = [1000.0]
    timer = SimpleNamespace(time=lambda: now[0])
    for module in (integration, inbox_module, capture):
        monkeypatch.setattr(module, "time", timer)
    host = plugin()
    owner = integration.source_scope(host, source())
    arrival = now[0]
    # A second event completes capture while the original event awaits Host APIs.
    now[0] += 1
    later = capture.capture_message(host, source(file_component("later.txt", b"later")), "q2")
    now[0] += 1
    first = capture.capture_message(
        host, source(file_component("current.txt", b"current")), "q1", captured_at=arrival
    )
    store = AssetStore.from_env()
    inbox = Inbox(store)
    visible = inbox.list(owner, before=first["created"], include_refs=first["inbox_refs"])
    assert [row["incoming_ref"] for row in visible] == first["inbox_refs"]
    current = await transfers.import_attachment(
        host, store, owner, first["inbox_refs"][0], before=first["created"],
        current_refs=first["inbox_refs"],
    )
    assert store.read(owner, current["asset_ref"]) == b"current"
    with pytest.raises(AssetError):
        await transfers.import_attachment(
            host, store, owner, later["inbox_refs"][0], before=first["created"],
            current_refs=first["inbox_refs"],
        )


def test_inbox_expiry_and_scope_are_enforced(monkeypatch, tmp_path):
    now = [1000.0]
    monkeypatch.setattr(inbox_module, "time", SimpleNamespace(time=lambda: now[0]))
    inbox = Inbox(AssetStore(tmp_path))
    ref = inbox.add(scope(), {"name": "file.txt", "size": 3, "file_id": "native-id"})
    with pytest.raises(AssetError):
        inbox.get(replace(scope(), actor_id="other"), ref)
    now[0] += inbox_module.RETENTION + 1
    assert not inbox.list(scope())
    with pytest.raises(AssetError):
        inbox.get(scope(), ref)


def test_direct_attachment_wins_over_quote_without_downloading_old_target(monkeypatch, tmp_path):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    quoted = SimpleNamespace(type="Quote", group_id="100", target_id=None,
                             origin=[file_component("old.txt", b"old")])
    host = plugin()
    result = capture.capture_message(
        host, source(quoted, file_component("current.txt", b"current")), "q1"
    )
    owner = integration.source_scope(host, source())
    inbox = Inbox(AssetStore.from_env())
    assert len(result["inbox_refs"]) == 1 and not result["unresolved_quote"]
    assert inbox.get(owner, result["inbox_refs"][0])["name"] == "current.txt"
    assert len(inbox.list(owner)) == 1


@pytest.mark.parametrize("origin,group", [
    (None, "100"),
    ([file_component()], "999"),
    ([SimpleNamespace(type="Plain", text="unresolved original attachment")], "100"),
])
def test_unresolved_or_foreign_quote_does_not_silently_select_history(monkeypatch, tmp_path, origin, group):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    host = plugin()
    capture.capture_message(host, source(file_component("earlier.txt", b"old")), "q0")
    quote = SimpleNamespace(type="Quote", group_id=group, target_id=None, origin=origin)
    current = capture.capture_message(host, source(quote), "q1")
    assert not current["inbox_refs"] and current["unresolved_quote"] is True


@pytest.mark.asyncio
async def test_qq_import_uses_original_native_identity_and_caches_verified_bytes(monkeypatch, tmp_path):
    store, owner = AssetStore(tmp_path), scope()
    inbox = Inbox(store)
    ref = inbox.add(owner, {"name": "data.txt", "size": 3, "file_id": "native-id"})
    host = SimpleNamespace(call_platform_api=AsyncMock(return_value={"url": "https://qq.example/file"}))
    fetch = AsyncMock(return_value=Fetched("https://qq.example/file", b"abc", "text/plain"))
    monkeypatch.setattr(transfers, "fetch_public_file", fetch)
    result = await transfers.import_attachment(host, store, owner, ref, before=1e20)
    host.call_platform_api.assert_awaited_once_with(
        "bot-a", "call_platform_api", {
            "action": "get_group_file_url", "params": {"file_id": "native-id", "group_id": "100"}
        }
    )
    assert store.read(owner, result["asset_ref"]) == b"abc"
    repeated = await transfers.import_attachment(host, store, owner, ref, before=1e20)
    assert result["asset_ref"] == repeated["asset_ref"] and fetch.await_count == 1


@pytest.mark.asyncio
async def test_import_length_mismatch_does_not_register_corrupt_asset(monkeypatch, tmp_path):
    store, owner = AssetStore(tmp_path), scope()
    ref = Inbox(store).add(owner, {"name": "data.txt", "size": 8, "url": "https://qq.example/file"})
    monkeypatch.setattr(transfers, "fetch_public_file", AsyncMock(return_value=Fetched(
        "https://qq.example/file", b"short", "text/plain"
    )))
    with pytest.raises(AssetError) as caught:
        await transfers.import_attachment(object(), store, owner, ref, before=1e20)
    assert caught.value.code == "integrity_error" and not store.list(owner)["assets"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError("private-transport"), asyncio.CancelledError()])
async def test_restart_does_not_repeat_sent_or_ambiguous_upload(tmp_path, failure):
    store, owner = AssetStore(tmp_path), scope()
    item = store.put(owner, "notes.txt", b"payload")
    current_api = SimpleNamespace(reply=AsyncMock(side_effect=failure))
    context = {"request_key": "trusted-request", "created": time.time()}
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await transfers.send_attachment(store, owner, context, current_api, item["asset_ref"])
    elif failure is not None:
        with pytest.raises(AssetError) as caught:
            await transfers.send_attachment(store, owner, context, current_api, item["asset_ref"])
        assert caught.value.code == "delivery_unconfirmed"
        assert "private-transport" not in str(caught.value)
    else:
        assert (await transfers.send_attachment(
            store, owner, context, current_api, item["asset_ref"]
        ))["status"] == "sent"
    restarted = AssetStore(tmp_path)
    if failure is None:
        assert (await transfers.send_attachment(
            restarted, owner, context, current_api, item["asset_ref"]
        ))["already_sent"]
    else:
        with pytest.raises(AssetError) as caught:
            await transfers.send_attachment(restarted, owner, context, current_api, item["asset_ref"])
        assert caught.value.code == "delivery_unconfirmed"
    current_api.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_subprocess_text_read_export_read_and_archive_extract(tmp_path):
    store, owner = AssetStore(tmp_path), scope()
    original = store.put(owner, "source.txt", "第一段\n第二段中文\n".encode())
    read = await run_operation(store, owner, "read", {"asset_ref": original["asset_ref"]})
    text = "".join(section["text"] for section in read["sections"])
    assert text == "第一段\n第二段中文\n"
    generated = await run_operation(store, owner, "create", {
        "format": "txt", "filename": "result.txt", "payload": {"text": text + "追加结果\n"}
    })
    ref = generated["artifact"]["asset_ref"]
    assert generated["result_refs"] == [ref] and generated["artifact"]["origin"] == "generated"
    reread = await run_operation(store, owner, "read", {"asset_ref": ref})
    assert "追加结果" in "".join(section["text"] for section in reread["sections"])
    archive = await run_operation(store, owner, "create", {
        "format": "zip", "filename": "bundle.zip",
        "payload": {"files": [{"name": "texts/result.txt", "asset_ref": ref}]},
    })
    extracted = await run_operation(store, owner, "extract", {
        "asset_ref": archive["artifact"]["asset_ref"], "member": "texts/result.txt",
        "filename": "extracted.txt",
    })
    assert store.read(owner, extracted["artifact"]["asset_ref"]) == store.read(owner, ref)
    assert extracted["artifact"]["origin"] == "archive"


@pytest.mark.asyncio
async def test_subprocess_refuses_other_scope_before_starting_worker(monkeypatch, tmp_path):
    store, owner = AssetStore(tmp_path), scope()
    item = store.put(owner, "private.txt", b"private")
    start = AsyncMock(side_effect=AssertionError("must not launch"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    with pytest.raises(AssetError):
        await run_operation(store, replace(owner, actor_id="foreign"), "read", {"asset_ref": item["asset_ref"]})
    start.assert_not_awaited()
