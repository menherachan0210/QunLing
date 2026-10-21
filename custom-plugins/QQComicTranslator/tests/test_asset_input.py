"""Real assets -> existing comic job -> LabelPlus; model/QQ are local stubs."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

import pytest
from PIL import Image
import yaml

from pkg import asset_input, tools
from pkg.archive import ArchiveError, ComicArchive
from pkg.asset_input import asset_parameters, prepare_assets
from pkg.jobs import JobManager, scope_key
from qunling.assets import AssetError, AssetStore
from qunling.capture import capture_message
from qunling.inbox import Inbox
from qunling.integration import CONTEXT_KEY, descriptor, source_scope


def image_bytes(format="PNG", color="white"):
    output = io.BytesIO()
    Image.new("RGB", (48, 64), color).save(output, format=format)
    return output.getvalue()


class Plugin:
    def __init__(self):
        self.storage, self.sends = {}, []
        self.plugin_runtime_handler = object()
        self.jobs = JobManager(self)
        self.invoke_llm = AsyncMock(return_value=SimpleNamespace(
            tool_calls=[], content=json.dumps({
                "status": "ok", "labels": [
                    {"x": 0.125, "y": 0.75, "group": 1, "text": "合成中文译文"}
                ],
            }),
        ))

    def get_installation_binding(self):
        return SimpleNamespace(
            instance_uuid="instance", workspace_uuid="workspace",
            installation_uuid="comic", runtime_revision=1, placement_generation=1,
        )

    def get_config(self):
        return {"model_uuid": "synthetic-vision"}

    async def set_plugin_storage(self, key, value):
        self.storage[key] = value

    async def get_plugin_storage(self, key):
        return self.storage.get(key, b"")

    async def get_plugin_storage_keys(self):
        return list(self.storage)

    async def delete_plugin_storage(self, key):
        self.storage.pop(key, None)

    def spawn_background(self, factory, timeout):
        assert timeout == 7200
        return asyncio.create_task(factory())

    async def send_message(self, bot, kind, target, chain):
        self.sends.append((bot, kind, target, chain))


def source(chain=()):
    return SimpleNamespace(
        bot_uuid="bot", launcher_type="group", launcher_id="123",
        sender_id="456", message_chain=list(chain),
    )


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    plugin = Plugin()
    context = descriptor(plugin, source(), "request-a")
    scope = source_scope(plugin, source())
    store = AssetStore.from_env()
    proxy = SimpleNamespace(
        query_uuid="request-a", get_query_vars=AsyncMock(return_value={CONTEXT_KEY: context}),
        get_bot_uuid=AsyncMock(return_value="bot"),
    )
    monkeypatch.setattr(tools, "QueryBasedAPIProxy", lambda **kw: proxy)

    def add(name, raw, *, owner=scope, grant=True):
        item = store.put(owner, name, raw)
        if grant:
            store.grant(owner, context["request_key"], item["asset_ref"])
        return item["asset_ref"]

    return SimpleNamespace(
        plugin=plugin, context=context, scope=scope, store=store, api=proxy,
        add=add, tool=SimpleNamespace(plugin=plugin),
    )


async def start(env, refs):
    return json.loads(await tools.execute_assets(
        env.tool, {"asset_refs": refs}, 1, "request-a"
    ))


async def finish(env):
    session = SimpleNamespace(bot_uuid="bot", chat_type="group", chat_id="123", sender_id="456")
    await env.plugin.jobs.tasks[scope_key(session)]
    assert len(env.plugin.sends) == 1
    bot, kind, target, chain = env.plugin.sends[0]
    assert (bot, kind, target) == ("bot", "group", "123")
    return base64.b64decode(chain[0].base64).decode("utf-8-sig"), chain[0].name


@pytest.mark.asyncio
async def test_single_image_reaches_real_labelplus_chain_with_original_name(env):
    # Real content detection accepts PNG bytes under a common JPEG filename.
    ref = env.add("原稿.jpeg", image_bytes())
    result = await start(env, [ref])
    assert result["status"] == "queued" and result["total_pages"] == 1
    text, name = await finish(env)
    assert text.startswith("1,0\r\n-\r\n框内\r\n框外\r\n-")
    assert ">>>>>>>>[原稿.jpeg]<<<<<<<<" in text
    assert "[0.125,0.750,1]\r\n合成中文译文" in text
    assert name == "原稿_LabelPlus.txt"
    assert env.plugin.invoke_llm.await_count == 1
    assert (await start(env, [ref]))["status"] == "completed"
    assert env.plugin.invoke_llm.await_count == 1 and len(env.plugin.sends) == 1


@pytest.mark.asyncio
async def test_multiple_images_naturally_sort_and_keep_original_bytes_and_names(env):
    second_raw, tenth_raw = image_bytes(color="red"), image_bytes(color="blue")
    tenth = env.add("page10.png", tenth_raw)
    second = env.add("page2.png", second_raw)
    prepared = prepare_assets(env.store, env.scope, [tenth, second], env.context)
    repeated = prepare_assets(env.store, env.scope, [second, tenth], env.context)
    assert prepared.raw == repeated.raw and prepared.page_names == ("page2.png", "page10.png")
    with zipfile.ZipFile(io.BytesIO(prepared.raw)) as archive:
        assert [archive.read(info) for info in archive.infolist()] == [second_raw, tenth_raw]
    assert (await start(env, [tenth, second]))["total_pages"] == 2
    text, name = await finish(env)
    assert text.index("[page2.png]") < text.index("[page10.png]")
    assert name == "漫画图片合集_LabelPlus.txt"
    assert env.plugin.invoke_llm.await_count == 2


@pytest.mark.asyncio
async def test_archive_asset_routes_real_zip_bytes_to_existing_pipeline(env):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("chapter/001.png", image_bytes())
    ref = env.add("actual-archive.bin", output.getvalue())
    adapted = prepare_assets(env.store, env.scope, [ref], env.context)
    assert adapted.raw == output.getvalue() and adapted.page_names is None
    assert (await start(env, [ref]))["status"] == "queued"
    text, _ = await finish(env)
    assert "[001.png]" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"not an image", b"%PDF-1.7", image_bytes()[:40]])
async def test_bad_or_mislabeled_second_image_rejects_entire_batch_before_model(env, payload):
    good = env.add("001.png", image_bytes())
    bad = env.add("002.png", payload)
    result = await start(env, [good, bad])
    assert result["status"] == "error"
    assert not env.plugin.jobs.tasks and not env.plugin.storage and not env.plugin.sends
    env.plugin.invoke_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_animation_is_not_silently_reduced_to_first_frame(env):
    output = io.BytesIO()
    frames = [Image.new("RGB", (48, 64), color) for color in ("red", "blue")]
    frames[0].save(output, format="PNG", save_all=True, append_images=frames[1:], duration=100)
    result = await start(env, [env.add("animated.png", output.getvalue())])
    assert result["status"] == "error" and "多帧" in result["error"]
    env.plugin.invoke_llm.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("workspace_id", "foreign"), ("bot_id", "other-bot"),
    ("conversation_id", "group:999"), ("actor_id", "other-sender"),
])
async def test_cross_scope_assets_never_reach_archive_or_model(env, field, value):
    foreign = replace(env.scope, **{field: value})
    ref = env.add("foreign.png", image_bytes(), owner=foreign)
    assert (await start(env, [ref]))["status"] == "error"
    env.plugin.invoke_llm.assert_not_awaited()
    assert not env.plugin.jobs.tasks


@pytest.mark.asyncio
async def test_same_scope_later_ungranted_asset_is_hidden_from_original_request(env):
    later = env.add("later.png", image_bytes(), grant=False)
    result = await start(env, [later])
    assert result["status"] == "error" and "可见范围" in result["error"]
    env.plugin.invoke_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_quoted_image_import_can_feed_comic_without_guessing_other_image(env):
    original = image_bytes(color="blue")
    quote = SimpleNamespace(
        type="Quote", group_id="123", target_id=None,
        origin=[SimpleNamespace(
            type="File", name="quoted.png", size=len(original), id="", url="",
            base64=base64.b64encode(original).decode(),
        )],
    )
    context = capture_message(env.plugin, source([quote]), "request-a")
    env.api.get_query_vars.return_value = {CONTEXT_KEY: context}
    entry = Inbox(env.store).get(
        env.scope, context["inbox_refs"][0], before=context["created"],
        include_refs=context["inbox_refs"],
    )
    assert not context["unresolved_quote"]
    assert (await start(env, [entry["asset_ref"]]))["status"] == "queued"
    text, _ = await finish(env)
    assert "[quoted.png]" in text


def test_expired_asset_is_rejected(env):
    ref = env.add("old.png", image_bytes())
    with env.store._connect() as db:
        db.execute("UPDATE assets SET expires=0 WHERE ref=?", (ref,))
    with pytest.raises(AssetError):
        prepare_assets(env.store, env.scope, [ref], env.context)


@pytest.mark.parametrize("names", [("same.png", "SAME.png"), ("é.png", "e\u0301.png")])
def test_conflicting_original_filenames_are_rejected_instead_of_renamed(env, names):
    refs = [
        env.add(names[0], image_bytes(color="red")),
        env.add(names[1], image_bytes(color="blue")),
    ]
    with pytest.raises(ArchiveError, match="同名"):
        prepare_assets(env.store, env.scope, refs, env.context)


def test_all_asset_metadata_is_budgeted_before_any_image_is_decoded(env, monkeypatch):
    refs = [env.add("a.png", image_bytes()), env.add("b.png", image_bytes(color="blue"))]
    with env.store._connect() as db:
        db.execute("UPDATE assets SET size=?", (21 * 1024 * 1024,))
    monkeypatch.setattr(env.store, "read", lambda *a: pytest.fail("budget before bytes"))
    with pytest.raises(ArchiveError, match="40 MiB"):
        prepare_assets(env.store, env.scope, refs, env.context)


def test_pixel_budget_rejects_input_before_building_job(env, monkeypatch):
    ref = env.add("large.png", image_bytes())
    monkeypatch.setattr(asset_input, "MAX_PIXELS", 100)
    with pytest.raises(ArchiveError, match="像素"):
        prepare_assets(env.store, env.scope, [ref], env.context)


@pytest.mark.asyncio
async def test_cancelled_asset_precheck_never_starts_background_paid_work(env, monkeypatch):
    ref = env.add("page.png", image_bytes())
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = asset_input.prepare_assets

    def delayed(*args):
        entered.set()
        assert release.wait(3)
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(asset_input, "prepare_assets", delayed)
    task = asyncio.create_task(start(env, [ref]))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 3)
    env.plugin.invoke_llm.assert_not_awaited()
    assert not env.plugin.jobs.tasks and not env.plugin.storage


@pytest.mark.parametrize("signature", [b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00", b"7z\xbc\xaf\x27\x1c"])
def test_native_archive_asset_signature_routes_without_reencoding(env, signature):
    raw = signature + b"synthetic-header-routing-only"
    ref = env.add("archive.bin", raw)
    prepared = prepare_assets(env.store, env.scope, [ref], env.context)
    assert prepared.page_names is None and prepared.raw == raw
    # Validation/extraction stays in existing open_archive; this routing test
    # deliberately does not claim the synthetic bytes are a real RAR/7z.


def test_mixed_archive_and_loose_images_fail_before_jobs(env):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("a.png", image_bytes())
    refs = [env.add("archive.zip", archive.getvalue()), env.add("b.png", image_bytes())]
    with pytest.raises(ArchiveError, match="单独选择"):
        prepare_assets(env.store, env.scope, refs, env.context)


@pytest.mark.parametrize("params", [
    {}, {"asset_refs": []}, {"asset_refs": "asset_" + "1" * 32},
    {"asset_refs": ["asset_" + "1" * 32] * 2},
    {"asset_refs": ["asset_" + f"{i:032x}" for i in range(101)]},
    {"asset_refs": ["https://example.test/a.png"]}, {"asset_refs": ["C:/local.png"]},
    {"asset_refs": ["asset_" + "1" * 32], "recipient": "other"},
])
def test_only_bounded_opaque_asset_refs_are_valid_parameters(params):
    with pytest.raises(ArchiveError):
        asset_parameters(params)


def test_internal_page_map_is_valid_for_existing_comic_parser(env):
    ref = env.add("unusual.name", image_bytes())
    result = prepare_assets(env.store, env.scope, [ref], env.context)
    with ComicArchive(result.raw) as archive:
        assert len(archive.pages) == 1 and archive.prepare_page(0).width == 48
    assert result.page_names == ("unusual.name",)


def test_tool_schema_contains_no_model_supplied_paths_or_destinations():
    schema = yaml.safe_load(
        (Path(__file__).parents[1] / "components/tools/start_comic_from_assets.yaml").read_text("utf-8")
    )["spec"]["parameters"]
    assert set(schema["properties"]) == {"asset_refs", "password"}
    assert schema["additionalProperties"] is False and schema["required"] == ["asset_refs"]
