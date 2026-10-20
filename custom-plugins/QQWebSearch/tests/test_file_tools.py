from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from pkg import context as state
from pkg import file_tools, tools
from pkg.context import QueryContext, QueryStore
from pkg.errors import WebError
from qunling.assets import AssetError, AssetStore
from qunling.scope import Scope


def attachment(raw=b"public attachment", name="example.txt"):
    return {
        "data": raw,
        "name": name,
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source_url": "https://files.example/example.txt",
        "content_type": "text/plain",
    }


def fixture():
    store = QueryStore()
    context = QueryContext(1, "first")
    store.entries["first"] = context
    plugin = SimpleNamespace(web_queries=store)
    return plugin, context


@pytest.mark.asyncio
async def test_download_prepares_a_query_scoped_ref_without_sending(monkeypatch):
    plugin, context = fixture()
    download = AsyncMock(return_value=attachment())
    monkeypatch.setattr(file_tools, "download_file", download)
    result = await file_tools.prepare_file(
        plugin, context, {"url": "https://files.example/example.txt"}
    )
    assert result["status"] == "downloaded"
    assert result["file_ref"].startswith("webfile_")
    assert context.files[result["file_ref"]]["data"] == b"public attachment"
    assert "data" not in result and "base64" not in result
    assert context.file_delivery == {}
    download.assert_awaited_once_with("https://files.example/example.txt", None)
    assert result["shared_asset_unavailable"]["code"] == "shared_asset_unavailable"


@pytest.mark.asyncio
async def test_download_registers_shared_asset_with_only_verified_scope(monkeypatch, tmp_path):
    plugin, context = fixture()
    item = attachment()
    scope = Scope("workspace", "bot", "group:trusted", "sender")
    api = SimpleNamespace(reply=AsyncMock())
    resolve = AsyncMock(return_value={"scope": scope.as_dict(), "created": 1, "request_key": "first"})
    monkeypatch.setattr(file_tools, "query_context", resolve)
    monkeypatch.setattr(file_tools, "download_file", AsyncMock(return_value=item))
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    result = await file_tools.prepare_file(
        plugin, context, {"url": item["source_url"]}, api=api
    )
    assert result["file_ref"] in context.files
    assert result["asset_ref"].startswith("asset_")
    resolve.assert_awaited_once_with(plugin, api)
    store = AssetStore.from_env()
    assert store.read(scope, result["asset_ref"]) == item["data"]
    assert store.get(scope, result["asset_ref"])["origin"] == "web"
    with pytest.raises(AssetError):
        store.read(Scope("workspace", "bot", "group:other", "sender"), result["asset_ref"])
    assert "shared_asset_unavailable" not in result
    api.reply.assert_not_awaited()
    assert not context.file_delivery


@pytest.mark.asyncio
async def test_missing_shared_scope_keeps_original_sendable_file_without_guessing_identity(monkeypatch):
    plugin, context = fixture()
    monkeypatch.setattr(file_tools, "download_file", AsyncMock(return_value=attachment()))
    monkeypatch.setattr(file_tools, "query_context", AsyncMock(side_effect=AssetError(
        "scope_unavailable", "synthetic-private-context"
    )))
    create = SimpleNamespace(from_env=lambda: pytest.fail("unverified scope must not open store"))
    monkeypatch.setattr(file_tools, "AssetStore", create)
    api = SimpleNamespace(reply=AsyncMock())
    result = await file_tools.prepare_file(
        plugin, context, {"url": "https://files.example/example.txt"}, api=api
    )
    assert "asset_ref" not in result and result["file_ref"] in context.files
    assert "synthetic-private-context" not in json.dumps(result)
    assert result["shared_asset_unavailable"]["code"] == "shared_asset_unavailable"
    assert (await file_tools.send_file(context, api, {"file_ref": result["file_ref"]}))["status"] == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("first_sender", ["web", "attachment"])
@pytest.mark.parametrize("failure", [False, True])
async def test_shared_and_web_delivery_cannot_repeat_each_other(monkeypatch, tmp_path, first_sender, failure):
    from qunling.transfers import send_attachment
    plugin, context = fixture()
    owner = Scope("workspace", "bot", "group:trusted", "sender")
    shared = {"scope": owner.as_dict(), "created": 1, "request_key": "first"}
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(file_tools, "query_context", AsyncMock(return_value=shared))
    monkeypatch.setattr(file_tools, "download_file", AsyncMock(return_value=attachment()))
    api = SimpleNamespace(reply=AsyncMock(side_effect=RuntimeError("private") if failure else None))
    result = await file_tools.prepare_file(plugin, context, {"url": "https://files.example/a.txt"}, api=api)
    store = AssetStore.from_env()

    async def web_send():
        return await file_tools.send_file(context, api, {"file_ref": result["file_ref"]}, plugin=plugin)

    async def attachment_send():
        return await send_attachment(store, owner, shared, api, result["asset_ref"])

    calls = [web_send, attachment_send] if first_sender == "web" else [attachment_send, web_send]
    if failure:
        for call in calls:
            with pytest.raises((WebError, AssetError)):
                await call()
    else:
        assert (await calls[0]())["status"] == "sent"
        assert (await calls[1]())["status"] == "sent"
    assert api.reply.await_count == 1


@pytest.mark.asyncio
async def test_duplicate_shared_send_does_not_downgrade_confirmed_receipt(monkeypatch, tmp_path):
    plugin, context = fixture()
    owner = Scope("workspace", "bot", "group:trusted", "sender")
    shared = {"scope": owner.as_dict(), "created": 1, "request_key": "first"}
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(file_tools, "query_context", AsyncMock(return_value=shared))
    monkeypatch.setattr(file_tools, "download_file", AsyncMock(return_value=attachment()))
    api = SimpleNamespace(reply=AsyncMock())
    result = await file_tools.prepare_file(plugin, context, {"url": "https://files.example/a.txt"}, api=api)
    await file_tools.send_file(context, api, {"file_ref": result["file_ref"]}, plugin=plugin)
    with pytest.raises(WebError, match="重复"):
        await file_tools.send_file(context, api, {"file_ref": result["file_ref"]}, plugin=plugin)
    assert context.file_delivery[result["sha256"]] == "sent"
    assert AssetStore.from_env().delivery(owner, "first", result["asset_ref"]) == "sent"
    api.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_shared_send_scope_failure_does_not_claim_delivery(monkeypatch, tmp_path):
    plugin, context = fixture()
    owner = Scope("workspace", "bot", "group:trusted", "sender")
    shared = {"scope": owner.as_dict(), "created": 1, "request_key": "first"}
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    resolve = AsyncMock(return_value=shared)
    monkeypatch.setattr(file_tools, "query_context", resolve)
    monkeypatch.setattr(file_tools, "download_file", AsyncMock(return_value=attachment()))
    api = SimpleNamespace(reply=AsyncMock())
    result = await file_tools.prepare_file(plugin, context, {"url": "https://files.example/a.txt"}, api=api)
    resolve.side_effect = AssetError("scope_unavailable", "请求上下文暂不可用。")
    with pytest.raises(WebError):
        await file_tools.send_file(context, api, {"file_ref": result["file_ref"]}, plugin=plugin)
    assert not context.file_delivery
    assert AssetStore.from_env().delivery(owner, "first", result["asset_ref"]) is None
    api.reply.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        None,
        {},
        {"url": None},
        {"url": ""},
        {"url": "https://a.example", "filename": None},
        {"url": "https://a.example", "filename": ""},
        {"url": "https://a.example", "filename": "a" * 181},
        {"url": "https://a.example", "group_id": "foreign"},
        {"url": "https://a.example", "path": "/private"},
    ],
)
async def test_bad_download_parameters_never_start_network(monkeypatch, params):
    plugin, context = fixture()
    download = AsyncMock()
    monkeypatch.setattr(file_tools, "download_file", download)
    with pytest.raises(WebError):
        await file_tools.prepare_file(plugin, context, params)
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_failure_does_not_cache_or_refund_limit(monkeypatch):
    plugin, context = fixture()
    download = AsyncMock(side_effect=WebError("download failed"))
    monkeypatch.setattr(file_tools, "download_file", download)
    for _ in range(3):
        with pytest.raises(WebError):
            await file_tools.prepare_file(
                plugin, context, {"url": "https://files.example/file.txt"}
            )
    with pytest.raises(WebError, match="上限"):
        await file_tools.prepare_file(
            plugin, context, {"url": "https://files.example/file.txt"}
        )
    assert download.await_count == 3
    assert not context.files and not context.file_delivery


@pytest.mark.asyncio
async def test_upload_uses_bytes_and_trusted_query_reply_not_url_or_recipient():
    plugin, context = fixture()
    item = attachment()
    ref = plugin.web_queries.remember_file(context, item)
    api = SimpleNamespace(reply=AsyncMock())
    result = await file_tools.send_file(context, api, {"file_ref": ref})
    assert result["status"] == "sent"
    chain = api.reply.await_args.args[0]
    assert [part.type for part in chain] == ["Plain", "File"]
    assert chain[1].name == item["name"] and chain[1].size == item["size"]
    assert base64.b64decode(chain[1].base64) == item["data"]
    assert not chain[1].url
    assert api.reply.await_args.kwargs == {"quote_origin": False}
    assert context.file_delivery[item["sha256"]] == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"file_ref": None},
        {"file_ref": []},
        {"file_ref": "webfile_unknown"},
        {"file_ref": "https://files.example/a.txt"},
        {"file_ref": "anything", "group_id": "foreign"},
    ],
)
async def test_invalid_ref_or_custom_recipient_never_sends(params):
    _, context = fixture()
    api = SimpleNamespace(reply=AsyncMock())
    with pytest.raises(WebError):
        await file_tools.send_file(context, api, params)
    api.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_ref_from_other_query_is_not_available():
    plugin, first = fixture()
    second = QueryContext(2, "second")
    ref = plugin.web_queries.remember_file(first, attachment())
    api = SimpleNamespace(reply=AsyncMock())
    with pytest.raises(WebError):
        await file_tools.send_file(second, api, {"file_ref": ref})
    api.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_parallel_duplicate_upload_is_claimed_once():
    plugin, context = fixture()
    ref = plugin.web_queries.remember_file(context, attachment())
    api = SimpleNamespace(reply=AsyncMock())
    results = await asyncio.gather(
        file_tools.send_file(context, api, {"file_ref": ref}),
        file_tools.send_file(context, api, {"file_ref": ref}),
        return_exceptions=True,
    )
    assert sum(isinstance(item, WebError) for item in results) == 1
    api.reply.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError("secret error")])
async def test_ambiguous_upload_does_not_retry_or_report_success(failure):
    plugin, context = fixture()
    item = attachment()
    ref = plugin.web_queries.remember_file(context, item)
    api = SimpleNamespace(reply=AsyncMock(side_effect=failure))
    with pytest.raises(WebError, match="未确认") as exc:
        await file_tools.send_file(context, api, {"file_ref": ref})
    assert "secret" not in str(exc.value)
    assert context.file_delivery[item["sha256"]] == "unconfirmed"
    with pytest.raises(WebError, match="重复"):
        await file_tools.send_file(context, api, {"file_ref": ref})
    api.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_upload_keeps_claim():
    plugin, context = fixture()
    item = attachment()
    ref = plugin.web_queries.remember_file(context, item)
    api = SimpleNamespace(reply=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await file_tools.send_file(context, api, {"file_ref": ref})
    assert context.file_delivery[item["sha256"]] == "unconfirmed"
    with pytest.raises(WebError, match="重复"):
        await file_tools.send_file(context, api, {"file_ref": ref})
    api.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_unpatched_runtime_reports_capacity_before_ambiguous_send(monkeypatch):
    plugin, context = fixture()
    ref = plugin.web_queries.remember_file(context, attachment())
    monkeypatch.setattr(file_tools, "MAX_MESSAGE_BYTES", 1)
    api = SimpleNamespace(reply=AsyncMock())
    with pytest.raises(WebError, match="补丁"):
        await file_tools.send_file(context, api, {"file_ref": ref})
    assert not context.file_delivery and context.counts == {}
    api.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_attachments_allowed_independently_of_image_send():
    plugin, context = fixture()
    context.delivery = "sent"
    api = SimpleNamespace(reply=AsyncMock())
    for index in range(2):
        ref = plugin.web_queries.remember_file(context, attachment(bytes([index])))
        assert (await file_tools.send_file(context, api, {"file_ref": ref}))[
            "status"
        ] == "sent"
    ref = plugin.web_queries.remember_file(context, attachment(b"third"))
    with pytest.raises(WebError, match="上限"):
        await file_tools.send_file(context, api, {"file_ref": ref})
    assert api.reply.await_count == 2


def test_downloads_deduplicate_bytes_even_if_renamed_or_from_another_url():
    plugin, context = fixture()
    first = plugin.web_queries.remember_file(context, attachment(name="one.txt"))
    second = plugin.web_queries.remember_file(context, attachment(name="two.txt"))
    assert first == second and len(context.files) == 1


@pytest.mark.asyncio
async def test_duplicate_download_reports_metadata_of_actual_cached_attachment(
    monkeypatch,
):
    plugin, context = fixture()
    first = attachment(name="original.txt")
    first_ref = plugin.web_queries.remember_file(context, first)
    renamed = {
        **attachment(name="renamed.txt"),
        "source_url": "https://mirror.example/a",
    }
    monkeypatch.setattr(file_tools, "download_file", AsyncMock(return_value=renamed))
    result = await file_tools.prepare_file(
        plugin, context, {"url": renamed["source_url"]}
    )
    assert result["file_ref"] == first_ref
    assert result["name"] == first["name"]
    assert result["source_url"] == first["source_url"]


def test_files_share_global_memory_budget_with_images(monkeypatch):
    monkeypatch.setattr(state, "MAX_BYTES", 5)
    plugin, context = fixture()
    second = QueryContext(2, "second")
    plugin.web_queries.entries["second"] = second
    plugin.web_queries.remember(context, {"data": b"123"})
    plugin.web_queries.remember_file(second, attachment(b"45"))
    with pytest.raises(WebError, match="缓存"):
        plugin.web_queries.remember(context, {"data": b"6"})
    with pytest.raises(WebError, match="缓存"):
        plugin.web_queries.remember_file(second, attachment(b"7"))


@pytest.mark.asyncio
async def test_execute_download_send_and_list_in_same_query(monkeypatch):
    variables = {}

    async def set_var(key, value):
        variables[key] = value

    api = SimpleNamespace(
        get_query_vars=AsyncMock(side_effect=lambda: variables.copy()),
        set_query_var=set_var,
        reply=AsyncMock(),
    )
    monkeypatch.setattr(tools, "QueryBasedAPIProxy", lambda **kwargs: api)
    monkeypatch.setattr(
        file_tools, "download_file", AsyncMock(return_value=attachment())
    )
    monkeypatch.setattr(
        tools,
        "list_file_links",
        AsyncMock(return_value={"url": "https://files.example", "links": []}),
    )
    plugin = SimpleNamespace(web_queries=QueryStore(), plugin_runtime_handler=object())
    tool = SimpleNamespace(plugin=plugin)
    listing = json.loads(
        await tools.execute(
            tool, "file_links", {"url": "https://files.example"}, 1, "a"
        )
    )
    assert listing["status"] == "ok"
    downloaded = json.loads(
        await tools.execute(
            tool,
            "file_download",
            {"url": "https://files.example/example.txt"},
            1,
            "a",
        )
    )
    assert downloaded["status"] == "downloaded"
    sent = json.loads(
        await tools.execute(
            tool, "file_send", {"file_ref": downloaded["file_ref"]}, 1, "a"
        )
    )
    assert sent["status"] == "sent"
    assert next(iter(plugin.web_queries.entries.values())).active == 0
    api.reply.assert_awaited_once()


def test_file_tool_schemas_expose_no_recipient_or_local_path():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "components/tools"
    for name, keys in [
        ("list_web_files", {"url"}),
        ("download_web_file", {"url", "filename"}),
        ("send_web_file", {"file_ref"}),
    ]:
        document = yaml.safe_load((root / f"{name}.yaml").read_text(encoding="utf-8"))
        parameters = document["spec"]["parameters"]
        assert parameters["additionalProperties"] is False
        assert set(parameters["properties"]) == keys
