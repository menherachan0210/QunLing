"""Real-byte handoff across search, shared file processing and QQ delivery."""
import asyncio
import base64
import io
import json
import time
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from pkg import materials, tools, media
from pkg.context import QueryContext, QueryStore
from pkg.errors import WebError
from qunling.assets import AssetError, AssetStore
from qunling.integration import CONTEXT_KEY, descriptor
from qunling.processing import run_operation
from qunling.scope import Scope
from qunling.transfers import send_attachment


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    binding = SimpleNamespace(instance_uuid="instance", workspace_uuid="workspace", placement_generation=1)
    plugin = SimpleNamespace(get_installation_binding=lambda: binding, web_queries=QueryStore())
    source = SimpleNamespace(bot_uuid="bot", launcher_type="group", launcher_id="123", sender_id="alice")
    shared = descriptor(plugin, source, "request")
    api = SimpleNamespace(query_uuid="request", get_query_vars=AsyncMock(return_value={CONTEXT_KEY: shared}),
                          get_bot_uuid=AsyncMock(return_value="bot"), reply=AsyncMock())
    context = QueryContext(1, "request")
    plugin.web_queries.entries["query"] = context
    return SimpleNamespace(plugin=plugin, api=api, shared=shared, context=context,
                           scope=Scope(**shared["scope"]), store=AssetStore.from_env())


def picture():
    output = io.BytesIO()
    frames = [Image.new("RGB", (180, 180), color) for color in ("red", "blue", "green")]
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=[100, 200, 300], loop=0)
    return output.getvalue()


@pytest.mark.asyncio
async def test_mixed_web_materials_become_real_zip_bytes_not_links(env):
    raw = picture()
    image = media.prepare_image(raw)
    image.update(source_title="Synthetic gallery", source_url="https://example.com/gallery")
    image_ref = env.plugin.web_queries.remember(env.context, image)
    page_ref = env.plugin.web_queries.remember_page(env.context, "https://example.com/article", {
        "url": "https://example.com/article", "title": "Article", "text": "First section",
        "full_text": "First section\nSecond section", "content_truncated": True, "quality": "readable",
    })
    search_ref = env.plugin.web_queries.remember_search(env.context, "research", {
        "answer": "Search summary", "sources": [{"url": "https://example.com/article", "title": "Article"}],
    })
    file_ref = env.plugin.web_queries.remember_file(env.context, {
        "name": "data.csv", "data": b"name,count\na,2\n", "sha256": "test", "source_url": "https://example.com/data.csv",
    })
    collected = await materials.collect_materials(env.plugin, env.context, env.api, {"refs": [image_ref, page_ref, file_ref, search_ref]})
    assert collected["status"] == "ok" and len(collected["materials"]) == 4
    for item in collected["materials"]:
        env.store.require_visible(env.scope, item["asset_ref"], env.shared)
    assert env.store.read(env.scope, collected["materials"][0]["asset_ref"]) == raw
    page = env.store.read(env.scope, collected["materials"][1]["asset_ref"]).decode()
    assert "Second section" in page and "截断" in page
    summary = env.store.read(env.scope, collected["materials"][3]["asset_ref"]).decode()
    assert "Search summary" in summary and "摘要" in summary
    files = [{"name": item["suggested_member"], "asset_ref": item["asset_ref"]} for item in collected["materials"]]
    files.append({"name": "sources.json", "asset_ref": collected["manifest"]["asset_ref"]})
    created = await run_operation(env.store, env.scope, "create", {
        "format": "zip", "filename": "research.zip", "payload": {"files": files},
    })
    ref = created["result_refs"][0]
    env.store.grant(env.scope, env.shared["request_key"], ref)
    env.api.reply.assert_not_awaited()
    await send_attachment(env.store, env.scope, env.shared, env.api, ref)
    attachment = env.api.reply.await_args.args[0].root[0]
    assert attachment.type == "File" and attachment.name == "research.zip"
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(attachment.base64))) as archive:
        assert archive.read(files[0]["name"]) == raw
        assert archive.read(files[2]["name"]) == b"name,count\na,2\n"
        assert "https://example.com/gallery" in archive.read("sources.json").decode()


@pytest.mark.asyncio
async def test_web_send_and_shared_send_share_one_receipt_and_preserve_gif(env):
    image = {**media.prepare_image(picture()), "source_url": "https://example.com/a.gif", "source_title": "Animation"}
    ref = env.plugin.web_queries.remember(env.context, image)
    asset = await materials.register_material(env.plugin, env.api, image, kind="image")
    first = await tools.send_images(env.context, env.api, {"image_refs": [ref]}, plugin=env.plugin)
    assert first["status"] == "sent"
    component = env.api.reply.await_args.args[0].root[-1]
    assert component.type == "Image"
    assert base64.b64decode(component.base64) == image["data"]
    second = await send_attachment(env.store, env.scope, env.shared, env.api, asset["asset_ref"])
    assert second["already_sent"]
    env.api.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_query_or_actor_cannot_borrow_web_materials(env):
    with pytest.raises(WebError):
        await materials.collect_materials(env.plugin, env.context, env.api, {"refs": ["webimg_foreign"]})
    image = {**media.prepare_image(picture()), "source_url": "https://example.com/a.gif"}
    asset = await materials.register_material(env.plugin, env.api, image, kind="image")
    foreign = Scope(env.scope.workspace_id, env.scope.bot_id, env.scope.conversation_id, "bob")
    with pytest.raises(AssetError):
        env.store.get(foreign, asset["asset_ref"])
    env.api.query_uuid = "different-query"
    with pytest.raises(AssetError):
        await materials.register_material(env.plugin, env.api, image, kind="image")


@pytest.mark.asyncio
async def test_slow_source_does_not_erase_successful_downloads():
    cancelled = asyncio.Event()
    async def fast():
        return {"data": b"usable"}
    async def slow():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    result = await tools._bounded_results([fast(), slow()], seconds=.02)
    assert result == [{"data": b"usable"}] and cancelled.is_set()


@pytest.mark.asyncio
async def test_known_image_url_reuses_source_and_registers_shared_asset(env, monkeypatch):
    env.plugin.get_config = lambda: {}
    search = AsyncMock()
    monkeypatch.setattr(tools, "search_web", search)
    monkeypatch.setattr(tools, "fetch_image", AsyncMock(return_value=media.prepare_image(picture())))
    async def inspect(plugin, query, candidates):
        return [{**item, "description": "sampled frames"} for item in candidates]
    monkeypatch.setattr(tools, "inspect_candidates", inspect)
    result = await tools.find_images(env.plugin, env.context, "动画", api=env.api,
                                     source_urls=["https://example.com/a.gif"], purpose="collect")
    assert result["status"] == "ok" and result["images"][0]["animated"]
    assert env.store.read(env.scope, result["images"][0]["asset_ref"]) == picture()
    assert result["_qunling_task"]["can_continue"]
    search.assert_not_awaited()
    env.api.reply.assert_not_awaited()
