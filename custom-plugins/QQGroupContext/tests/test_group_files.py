from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pkg import group_files as files
from pkg.errors import VisionError
from qunling.assets import AssetError, AssetStore, MAX_ASSET_BYTES
from qunling.network import Fetched
from qunling.scope import Scope


def file_row(number=1, **kwargs):
    return {"file_id": f"private-file-{number}", "file_name": f"翻译稿{number}.txt",
            "file_size": 4, "busid": 102, "upload_time": 123, **kwargs}


def folder_row(number=1):
    return {"folder_id": f"private-folder-{number}", "folder_name": f"目录{number}",
            "total_file_count": 3}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    context = SimpleNamespace(bot="bot", group="200", sender="300",
                              group_data={}, group_data_lock=asyncio.Lock())
    scope = Scope("workspace", "bot", "group:200", "300")
    shared = {"scope": scope.as_dict(), "request_key": "query", "created": 1}
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    access = AsyncMock()
    platform = AsyncMock(return_value={"files": [file_row()], "folders": [folder_row()]})
    fetch = AsyncMock(return_value=Fetched("https://example.org/file?secret=hidden", b"data", "text/plain"))
    monkeypatch.setattr(files, "validate_group_access", access)
    monkeypatch.setattr(files, "call_group_api", platform)
    monkeypatch.setattr(files, "fetch_public_file", fetch)
    monkeypatch.setattr(files, "query_context", AsyncMock(return_value=shared))
    return context, scope, shared, access, platform, fetch


async def run(setup, **params):
    return await files.group_files(None, setup[0], params, None)


@pytest.mark.asyncio
async def test_paging_retains_directory_snapshot_and_only_reveals_opaque_refs(setup):
    first = await run(setup, limit=1)
    second = await run(setup, offset=first["next_offset"], limit=1)
    assert first["entries"][0]["kind"] == "folder"
    assert second["entries"][0]["kind"] == "file"
    assert second["next_offset"] is None
    assert second["entries"][0]["size"] == 4
    assert "private-" not in json.dumps([first, second])
    assert "busid" not in json.dumps(second)
    setup[4].assert_awaited_once_with(None, setup[0], "get_group_root_files", {})
    assert setup[3].await_count == 2
    again = await run(setup, offset=1)
    assert again["entries"] == second["entries"]


@pytest.mark.asyncio
async def test_only_shown_folders_can_be_entered_and_filename_search_is_local(setup):
    first = await run(setup, search_text="翻译")
    assert first["search_scope"] == "current_folder"
    hidden = next(ref for ref, item in setup[0].group_data["files"]["entries"].items()
                  if item["public"]["kind"] == "folder")
    with pytest.raises(VisionError):
        await run(setup, folder_ref=hidden)
    listing = await run(setup)
    folder_ref = listing["entries"][0]["folder_ref"]
    setup[4].return_value = {"folders": [], "files": [file_row(2)]}
    folder = await run(setup, folder_ref=folder_ref, search_text="稿2")
    assert folder["entries"][0]["name"] == "翻译稿2.txt"
    setup[4].assert_awaited_with(None, setup[0], "get_group_files_by_folder", {"folder_id": "private-folder-1"})


@pytest.mark.asyncio
async def test_import_exact_selected_file_grants_actual_bytes_and_is_idempotent(setup):
    result = await run(setup, search_text="翻译")
    ref = result["entries"][0]["file_ref"]
    setup[4].return_value = {"url": "https://example.org/file?secret=hidden"}
    imported = await run(setup, action="import", file_ref=ref)
    asset = imported["asset"]
    store = AssetStore.from_env()
    assert store.read(setup[1], asset["asset_ref"]) == b"data"
    store.require_visible(setup[1], asset["asset_ref"], setup[2])
    with pytest.raises(AssetError):
        store.read(Scope("workspace", "bot", "group:201", "300"), asset["asset_ref"])
    with pytest.raises(AssetError):
        store.read(Scope("workspace", "bot", "group:200", "301"), asset["asset_ref"])
    assert asset["name"] == "翻译稿1.txt"
    assert "secret" not in json.dumps(imported)
    assert "private-file" not in json.dumps(imported)
    setup[4].assert_awaited_with(None, setup[0], "get_group_file_url", {"file_id": "private-file-1", "busid": 102})
    setup[5].assert_awaited_once_with("https://example.org/file?secret=hidden", max_bytes=MAX_ASSET_BYTES, timeout=45)
    assert (await run(setup, action="import", file_ref=ref))["asset"] == asset
    assert setup[5].await_count == 1


@pytest.mark.asyncio
async def test_unshown_forged_cross_query_and_cross_scope_files_rejected(setup):
    await run(setup, limit=1)
    ref = next(ref for ref, entry in setup[0].group_data["files"]["entries"].items()
               if entry["public"]["kind"] == "file")
    for bad in (ref, "gfile_forged", "private-file-1", "https://example.org/file"):
        with pytest.raises(VisionError):
            await run(setup, action="import", file_ref=bad)
    await run(setup, offset=1)
    other = SimpleNamespace(bot="bot", group="200", sender="300",
                            group_data={}, group_data_lock=asyncio.Lock())
    with pytest.raises(VisionError):
        await files.group_files(None, other, {"action": "import", "file_ref": ref}, None)
    for field, value in (("sender", "400"), ("group", "201"), ("bot", "other")):
        original = getattr(setup[0], field)
        setattr(setup[0], field, value)
        with pytest.raises(AssetError, match="会话不匹配"):
            await run(setup, action="import", file_ref=ref)
        setattr(setup[0], field, original)
    setup[5].assert_not_awaited()


@pytest.mark.asyncio
async def test_membership_failure_precedes_any_list_or_download(setup):
    setup[3].side_effect = VisionError("无法确认当前群成员权限。")
    with pytest.raises(VisionError):
        await run(setup)
    setup[4].assert_not_awaited()
    setup[5].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"group_id": "999"}, {"file_id": "raw"}, {"action": "delete"},
    {"action": "import", "file_ref": "x", "url": "https://example.org"},
    {"folder_ref": 42}, {"folder_ref": ""}, {"limit": True}, {"limit": 41},
    {"offset": -1}, {"search_text": " "}, {"search_text": "x" * 101},
    {"search_text": "x\n"}, {"action": "import"},
])
async def test_rejects_raw_targets_and_invalid_parameters(setup, params):
    with pytest.raises(VisionError):
        await run(setup, **params)
    setup[4].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, {}, {"files": []}, {"files": {}, "folders": []}])
async def test_missing_directory_data_is_not_reported_as_empty(setup, response):
    setup[4].return_value = response
    with pytest.raises(VisionError, match="未返回有效列表"):
        await run(setup)


@pytest.mark.asyncio
async def test_download_error_has_no_url_or_underlying_exception(setup):
    ref = (await run(setup, search_text="翻译"))["entries"][0]["file_ref"]
    setup[4].return_value = {"url": "https://example.org/?secret=hidden"}
    setup[5].side_effect = RuntimeError("https://internal/?password=secret")
    with pytest.raises(VisionError) as failure:
        await run(setup, action="import", file_ref=ref)
    assert "secret" not in str(failure.value) and "http" not in str(failure.value)
    assert not AssetStore.from_env().list(setup[1])["assets"]
    setup[4].return_value = {}
    with pytest.raises(VisionError, match="暂时无法下载"):
        await run(setup, action="import", file_ref=ref)


@pytest.mark.asyncio
async def test_file_size_and_names_checked_before_downloading(setup):
    setup[4].return_value = {"folders": [], "files": [
        file_row(1, file_size=MAX_ASSET_BYTES + 1), file_row(2, file_name="../secret.txt"),
    ]}
    result = await run(setup)
    with pytest.raises(VisionError, match="40 MiB"):
        await run(setup, action="import", file_ref=result["entries"][0]["file_ref"])
    with pytest.raises(AssetError, match="文件名"):
        await run(setup, action="import", file_ref=result["entries"][1]["file_ref"])
    setup[5].assert_not_awaited()


@pytest.mark.asyncio
async def test_bounds_index_directory_reads_and_failed_downloads(setup, monkeypatch):
    monkeypatch.setattr(files, "MAX_ENTRIES", 3)
    setup[4].return_value = {"folders": [], "files": [file_row(i) for i in range(10)]}
    result = await run(setup)
    assert result["status"] == "partial" and result["truncated"]
    assert len(result["entries"]) == 3
    assert len(setup[0].group_data["files"]["entries"]) == 3
    ref = result["entries"][0]["file_ref"]
    setup[4].return_value = {"url": "https://example.org/file"}
    setup[5].side_effect = RuntimeError("failed")
    for _ in range(files.MAX_IMPORT_ATTEMPTS):
        with pytest.raises(VisionError, match="下载失败"):
            await run(setup, action="import", file_ref=ref)
    with pytest.raises(VisionError, match="下载已达到预算"):
        await run(setup, action="import", file_ref=ref)
    assert setup[5].await_count == files.MAX_IMPORT_ATTEMPTS


@pytest.mark.asyncio
async def test_host_truncation_remains_visible_after_plugin_pagination(setup):
    from qunling.qq_group_api import _directory

    response = _directory({"folders": [], "files": [file_row(i) for i in range(2500)]}, "200")
    assert len(response["files"]) == 2000 and response["truncated"] is True
    setup[4].return_value = response
    first = await run(setup)
    last = await run(setup, offset=1990)
    assert first["status"] == last["status"] == "partial"
    assert first["truncated"] is last["truncated"] is True
    assert last["next_offset"] is None
    assert last["matching_entries"] == 2000


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"", b"dat", b"data with unexpected bytes"])
async def test_download_must_match_qq_reported_size_before_registering_asset(setup, body):
    ref = (await run(setup, search_text="翻译"))["entries"][0]["file_ref"]
    setup[4].return_value = {"url": "https://example.org/file"}
    setup[5].return_value = Fetched("https://example.org/file", body, "text/plain")
    with pytest.raises(AssetError) as failure:
        await run(setup, action="import", file_ref=ref)
    assert failure.value.code == "integrity_error"
    assert not AssetStore.from_env().list(setup[1])["assets"]
    assert not setup[0].group_data["files"]["imported"]


@pytest.mark.asyncio
async def test_concurrent_same_file_import_fetches_once(setup):
    ref = (await run(setup, search_text="翻译"))["entries"][0]["file_ref"]
    setup[4].return_value = {"url": "https://example.org/file"}
    outputs = await asyncio.gather(*(run(setup, action="import", file_ref=ref) for _ in range(2)))
    assert outputs[0]["asset"] == outputs[1]["asset"]
    setup[5].assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_entries_are_reported_and_never_create_refs(setup):
    setup[4].return_value = {"folders": [None], "files": [file_row(busid=None)]}
    result = await run(setup)
    assert result["status"] == "partial" and result["unavailable_entries"] == 2
    assert result["entries"] == []


@pytest.mark.asyncio
async def test_directory_budget_does_not_block_cached_pagination(setup, monkeypatch):
    monkeypatch.setattr(files, "MAX_DIRECTORIES", 2)
    setup[4].return_value = {"folders": [folder_row(1), folder_row(2)], "files": []}
    root = await run(setup)
    setup[4].return_value = {"folders": [], "files": []}
    await run(setup, folder_ref=root["entries"][0]["folder_ref"])
    with pytest.raises(VisionError, match="目录读取已达到预算"):
        await run(setup, folder_ref=root["entries"][1]["folder_ref"])
    assert (await run(setup))["entries"] == root["entries"]
    assert setup[4].await_count == 2


@pytest.mark.asyncio
async def test_failed_directory_reads_and_list_calls_are_bounded(setup, monkeypatch):
    monkeypatch.setattr(files, "MAX_DIRECTORIES", 1)
    setup[4].return_value = None
    for _ in range(3):
        with pytest.raises(VisionError, match="未返回有效列表"):
            await run(setup)
    with pytest.raises(VisionError, match="目录读取已达到预算"):
        await run(setup)
    assert setup[4].await_count == 3
    setup[0].group_data.clear()
    monkeypatch.setattr(files, "MAX_LIST_CALLS", 1)
    setup[4].return_value = {"files": [], "folders": []}
    await run(setup)
    with pytest.raises(VisionError, match="列表展示已达到预算"):
        await run(setup)


@pytest.mark.asyncio
async def test_import_cancelled_download_can_retry_and_releases_lock(setup):
    ref = (await run(setup, search_text="翻译"))["entries"][0]["file_ref"]
    setup[4].return_value = {"url": "https://example.org/file"}
    setup[5].side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await run(setup, action="import", file_ref=ref)
    assert not setup[0].group_data_lock.locked()
    setup[5].side_effect = None
    assert (await run(setup, action="import", file_ref=ref))["status"] == "ok"
