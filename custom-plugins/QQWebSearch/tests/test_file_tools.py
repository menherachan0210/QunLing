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
