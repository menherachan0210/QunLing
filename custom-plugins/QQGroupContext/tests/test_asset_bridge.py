from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from pkg import asset_bridge
from pkg.errors import VisionError
from qunling.assets import AssetError, AssetStore, image_name
from qunling.network import Fetched
from qunling.net_errors import NetworkError
from qunling.scope import Scope


def fixture(monkeypatch, tmp_path):
    scope = Scope("workspace", "bot", "group:200", "300")
    shared = {"scope": scope.as_dict(), "request_key": "query", "created": 1}
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(asset_bridge, "query_context", AsyncMock(return_value=shared))
    context = SimpleNamespace(
        bot="bot", group="200", sender="300", history={"messages": []},
        shown_images={"img_1", "img_2"}, image_urls={
            "img_1": "https://example.org/one.png", "img_2": "https://example.org/two.png",
        }, import_attempts=0, import_inflight=False, imported_assets={},
    )
    stream = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(stream, format="PNG")
    raw = stream.getvalue()
    fetch = AsyncMock(return_value=Fetched("https://example.org/one.png", raw, "image/png"))
    monkeypatch.setattr(asset_bridge, "fetch_public", fetch)
    return scope, shared, context, raw, fetch


@pytest.mark.asyncio
async def test_history_import_keeps_original_pixels_and_grants_current_request(monkeypatch, tmp_path):
    scope, shared, context, raw, fetch = fixture(monkeypatch, tmp_path)
    result = await asset_bridge.import_images(None, context, {"image_refs": ["img_1"]}, None)
    assert result["status"] == "ok"
    item = result["images"][0]
    store = AssetStore.from_env()
    assert store.read(scope, item["asset_ref"]) == raw
    store.require_visible(scope, item["asset_ref"], shared)
    assert item["name"].endswith(".png") and item["filename_generated"]
    again = await asset_bridge.import_images(None, context, {"image_refs": ["img_1"]}, None)
    assert again["images"] == result["images"]
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_history_import_preserves_partial_success(monkeypatch, tmp_path):
    _, _, context, raw, fetch = fixture(monkeypatch, tmp_path)
    fetch.side_effect = [Fetched("https://example.org/one.png", raw, "image/png"),
                         NetworkError("暂时无法访问。", code="network_timeout")]
    result = await asset_bridge.import_images(None, context, {"image_refs": ["img_1", "img_2"]}, None)
    assert result["status"] == "partial"
    assert [item["image_ref"] for item in result["images"]] == ["img_1"]
    assert result["failed_images"][0]["image_ref"] == "img_2"
    assert result["_qunling_task"]["can_continue"]


@pytest.mark.asyncio
async def test_same_image_imported_directly_and_from_history_reuses_asset(monkeypatch, tmp_path):
    scope, shared, context, raw, fetch = fixture(monkeypatch, tmp_path)
    store = AssetStore.from_env()
    direct = store.put(scope, image_name(raw), raw)
    store.grant(scope, shared["request_key"], direct["asset_ref"])
    result = await asset_bridge.import_images(None, context, {"image_refs": ["img_1"]}, None)
    assert result["images"][0]["asset_ref"] == direct["asset_ref"]
    assert result["images"][0]["name"] == direct["name"]
    assert len(store.list(scope)["assets"]) == 1
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_unshown_images_and_cross_chat_are_rejected_before_download(monkeypatch, tmp_path):
    _, _, context, _, fetch = fixture(monkeypatch, tmp_path)
    with pytest.raises(VisionError):
        await asset_bridge.import_images(None, context, {"image_refs": ["img_999"]}, None)
    context.group = "999"
    with pytest.raises(AssetError):
        await asset_bridge.import_images(None, context, {"image_refs": ["img_1"]}, None)
    fetch.assert_not_awaited()
