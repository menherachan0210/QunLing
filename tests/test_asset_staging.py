import asyncio
import io
import json
import tracemalloc
import zipfile

import pytest

from qunling.assets import AssetStore, AssetError
from qunling.processing import run_operation
from qunling.scope import Scope

SCOPE = Scope("workspace", "bot", "group:test", "member")


def test_large_asset_stages_in_chunks_and_checks_scope(tmp_path):
    store = AssetStore(tmp_path / "assets")
    content = b"abcdefgh" * (1024 * 1024)
    item = store.put(SCOPE, "original.bin", content)
    destination = tmp_path / "input"
    tracemalloc.start()
    try:
        staged = store.copy_to(SCOPE, item["asset_ref"], destination)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 3 * 1024 * 1024
    assert staged["sha256"] == item["sha256"] and destination.read_bytes() == content
    with pytest.raises(AssetError):
        store.copy_to(Scope("workspace", "bot", "group:other", "member"), item["asset_ref"], tmp_path / "other")
    assert not (tmp_path / "other").exists()


def test_bad_hash_removes_partial_copy_and_never_overwrites(tmp_path):
    store = AssetStore(tmp_path / "assets")
    item = store.put(SCOPE, "original.txt", b"original")
    (store.blobs / item["sha256"]).write_bytes(b"tampered")
    with pytest.raises(AssetError, match="完整性"):
        store.copy_to(SCOPE, item["asset_ref"], tmp_path / "partial")
    assert not (tmp_path / "partial").exists()
    (tmp_path / "existing").write_bytes(b"keep")
    with pytest.raises(AssetError):
        store.copy_to(SCOPE, item["asset_ref"], tmp_path / "existing")
    assert (tmp_path / "existing").read_bytes() == b"keep"


def test_zip_passes_small_reference_request_and_keeps_original_bytes(tmp_path, monkeypatch):
    import qunling.processing as processing
    store = AssetStore(tmp_path / "assets")
    content = b"original file\n" * 200000
    item = store.put(SCOPE, "original.txt", content)
    original, sizes = processing.asyncio.create_subprocess_exec, []
    async def observe(*args, **kwargs):
        path = kwargs["cwd"] / "request.json"
        sizes.append(path.stat().st_size)
        request = json.loads(path.read_text(encoding="utf-8"))
        assert "base64" not in request["args"]["payload"]["files"][0]
        assert request["zip_inputs"] == {"0": "zip-0.input"}
        return await original(*args, **kwargs)
    monkeypatch.setattr(processing.asyncio, "create_subprocess_exec", observe)
    result = asyncio.run(run_operation(store, SCOPE, "create", {
        "format": "zip", "filename": "bundle.zip", "payload": {"files": [
            {"name": "original.txt", "asset_ref": item["asset_ref"]}, {"name": "note.txt", "text": "note"}]}}))
    assert sizes[0] < 1024
    with zipfile.ZipFile(io.BytesIO(store.read(SCOPE, result["result_refs"][0]))) as archive:
        assert archive.read("original.txt") == content and archive.read("note.txt") == b"note"
