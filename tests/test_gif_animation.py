"""Render and decode real GIFs; verify isolation, revision and durable delivery."""
import asyncio
import base64
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from qunling.animation import render, revise_recipe, validate_recipe
from qunling.animation_preview import contact_sheet
from qunling.assets import AssetError, AssetStore
from qunling.delivery import HostDelivery
from qunling.jobs import JobService
from qunling.processing import run_operation
from qunling.scope import Scope

ROOT = Path(__file__).resolve().parents[1]
REF1, REF2 = "asset_" + "a" * 32, "asset_" + "b" * 32


def png(color):
    out = io.BytesIO()
    with Image.new("RGBA", (40, 40), color) as image:
        image.save(out, format="PNG")
    return out.getvalue()


def recipe():
    return {"width": 128, "height": 96, "duration_ms": 1000, "fps": 10, "layers": [
        {"type": "ellipse", "width": 20, "height": 20, "color": "#ff0000",
         "keyframes": [{"t": 0, "x": 20}, {"t": 1, "x": 100}]}]}


def test_code_shapes_have_real_motion_and_transparency(tmp_path):
    path = tmp_path / "actual.gif"
    info = render(recipe(), {}, path)
    assert info["animated"] and info["frame_count"] == 10
    assert info["duration_ms"] == 1000 and info["loop"] == 0
    with Image.open(path) as image:
        first = image.convert("RGBA")
        image.seek(9)
        last = image.convert("RGBA")
        assert first.getpixel((20, 48)) == (255, 0, 0, 255)
        assert last.getpixel((100, 48)) == (255, 0, 0, 255)
        assert first.getpixel((100, 48))[3] == 0
        assert last.getpixel((20, 48))[3] == 0


def test_slideshow_preserves_order_hold_times_and_loop(tmp_path):
    value = {"width": 64, "height": 64, "fps": 10, "loop": 3, "background": "white",
             "frames": [{"asset_ref": REF1, "duration_ms": 200}, {"asset_ref": REF2, "duration_ms": 600}]}
    path = tmp_path / "slides.gif"
    result = render(value, {REF1: png("red"), REF2: png("blue")}, path)
    assert result["duration_ms"] == 800 and result["loop"] == 3
    with Image.open(path) as image:
        image.seek(0)
        assert image.convert("RGB").getpixel((32, 32)) == (255, 0, 0)
        assert image.info["duration"] == 200
        image.seek(1)
        assert image.convert("RGB").getpixel((32, 32)) == (0, 0, 255)
        assert image.info["duration"] == 600


@pytest.mark.parametrize("effect", ["shake", "bounce", "pulse", "spin"])
def test_effects_produce_distinct_frames(effect, tmp_path):
    value = recipe()
    value["layers"] = [{"type": "rectangle", "width": 36, "height": 12, "effect": effect}]
    assert render(value, {}, tmp_path / "effect.gif")["frame_count"] > 1


def test_chinese_caption_renders_with_motion(tmp_path):
    value = recipe()
    value["layers"].append({"type": "text", "text": "急了！", "font_size": 18, "y": 78, "color": "blue"})
    result = render(value, {}, tmp_path / "caption.gif")
    assert result["animated"]
    with Image.open(tmp_path / "caption.gif") as image:
        pixels = list(image.convert("RGB").crop((25, 65, 105, 90)).getdata())
        assert any(b > 100 and r < 50 for r, g, b in pixels)


def test_revision_does_not_mutate_original_and_retimes_sources(tmp_path):
    original = validate_recipe(recipe())
    changed = revise_recipe(original, {"background": "white"}, .5)
    assert original["duration_ms"] == 1000 and original["background"] == "transparent"
    assert changed["duration_ms"] == 2000 and changed["source_speed"] == .5
    assert changed["layers"] == original["layers"]
    result = render(changed, {}, tmp_path / "slower.gif")
    assert result["duration_ms"] == 2000


def test_animated_input_is_not_flattened(tmp_path):
    source = tmp_path / "source.gif"
    render(recipe(), {}, source)
    value = {"width": 128, "height": 96, "duration_ms": 1000, "fps": 10,
             "layers": [{"type": "image", "asset_ref": REF1}]}
    assert render(value, {REF1: source.read_bytes()}, tmp_path / "copy.gif")["frame_count"] == 10


def test_short_slideshow_frames_are_not_lost_at_low_fps(tmp_path):
    value = {"width": 64, "height": 64, "fps": 1,
             "frames": [{"asset_ref": REF1, "duration_ms": 50}, {"asset_ref": REF2, "duration_ms": 50}]}
    result = render(value, {REF1: png("red"), REF2: png("blue")}, tmp_path / "short.gif")
    assert result["frame_count"] == 2 and result["frame_durations_ms"] == [50, 50]


@pytest.mark.parametrize("bad", [
    {"width": 4096}, {"fps": 24, "duration_ms": 10000}, {"fps": True},
    {"background": "not-a-color"}, {"python": "import os"}, {"duration_ms": float("nan")},
    {"frames": [{"asset_ref": "file:///secret"}]},
    {"layers": [{"type": "text", "text": "x" * 121}]},
    {"layers": [{"type": "image", "asset_ref": REF1, "scale": 99}]},
    {"layers": [{"type": "line", "keyframes": [{"t": .8}, {"t": .2}]}]},
])
def test_parameters_fail_before_spawning(bad):
    with pytest.raises(AssetError):
        validate_recipe({**recipe(), **bad})


def test_contact_sheet_contains_later_frames_and_timing(tmp_path):
    path = tmp_path / "move.gif"
    render(recipe(), {}, path)
    with Image.open(path) as image, contact_sheet(image) as sheet:
        assert sheet.width == 900 and sheet.height == 562
        # Different sample tiles show the red shape in different positions.
        first = sheet.crop((0, 64, 300, 160)).tobytes()
        last = sheet.crop((600, 324, 900, 420)).tobytes()
        assert first != last


@pytest.mark.asyncio
async def test_isolated_worker_keeps_scoped_recipe_and_refuses_foreign_input(tmp_path):
    store = AssetStore(tmp_path / "assets")
    scope = Scope("tenant", "bot", "group:1", "member")
    result = await run_operation(store, scope, "create", {"format": "gif", "filename": "结果.gif", "payload": recipe()})
    ref = result["result_refs"][0]
    assert result["animated"] and store.read(scope, ref).startswith(b"GIF89a")
    assert store.metadata(scope, ref)["gif_recipe"]["layers"] == recipe()["layers"]
    foreign = Scope("tenant", "bot", "group:2", "member")
    foreign_ref = store.put(foreign, "other.png", png("blue"))["asset_ref"]
    with pytest.raises(AssetError):
        await run_operation(store, scope, "create", {"format": "gif", "filename": "bad.gif",
            "payload": {"frames": [{"asset_ref": foreign_ref}]}})


def test_identical_pixels_can_retain_distinct_edit_recipes(tmp_path):
    store = AssetStore(tmp_path)
    scope = Scope("tenant", "bot", "group:1", "member")
    one = store.put(scope, "same.gif", b"synthetic", metadata={"gif_recipe": {"loop": 0}})
    two = store.put(scope, "same.gif", b"synthetic", metadata={"gif_recipe": {"loop": 1}})
    assert one["asset_ref"] != two["asset_ref"]
    assert one["sha256"] == two["sha256"]


def test_ai_frame_storage_failure_preserves_original_and_completed_frames(tmp_path):
    from qunling.ai_animation import frame_plan, save_frames
    store = AssetStore(tmp_path, max_assets=2)
    scope = Scope("tenant", "bot", "group:1", "member")
    stream = io.BytesIO()
    with Image.new("RGB", (128, 128), "blue") as sheet:
        sheet.save(stream, format="PNG")
    result = save_frames(store, scope, {"request_key": "gif-request"}, stream.getvalue(),
                         frame_plan({"gif_frames": ["wave", "smile"]}))
    assert result["status"] == "partial" and result["error_code"] == "storage_full"
    assert len(result["saved_refs"]) == 2 and len(result["frame_refs"]) == 1
    assert store.read(scope, result["storyboard_ref"]) == stream.getvalue()
    with pytest.raises(AssetError):
        store.read(Scope("tenant", "bot", "group:2", "member"), result["frame_refs"][0])


def test_six_ai_frames_follow_grid_order_and_exclude_unused_cells(tmp_path):
    from qunling.ai_animation import frame_plan, save_frames
    from PIL import ImageColor
    store = AssetStore(tmp_path)
    scope = Scope("tenant", "bot", "group:1", "member")
    stream = io.BytesIO()
    colors = ["red", "green", "blue", "yellow", "purple", "orange"]
    with Image.new("RGB", (192, 192), "black") as sheet:
        for index, color in enumerate(colors):
            row, column = divmod(index, 3)
            sheet.paste(color, (column * 64, row * 64, (column + 1) * 64, (row + 1) * 64))
        sheet.save(stream, format="PNG")
    result = save_frames(store, scope, {"request_key": "gif-request"}, stream.getvalue(),
                         frame_plan({"gif_frames": colors, "frame_duration_ms": 100}))
    assert len(result["frame_refs"]) == 6
    inputs = {ref: store.read(scope, ref) for ref in result["frame_refs"]}
    target = tmp_path / "six.gif"
    info = render(result["recipe"], inputs, target)
    assert info["frame_count"] == 6 and info["duration_ms"] == 600
    with Image.open(target) as gif:
        for index, color in enumerate(colors):
            gif.seek(index)
            assert gif.convert("RGB").getpixel((32, 32)) == ImageColor.getrgb(color)


@pytest.mark.asyncio
async def test_tool_background_worker_and_outbox_send_actual_gif_once(tmp_path, monkeypatch):
    import hashlib
    import json
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path / "data"))
    route = {"instance_uuid": "instance", "workspace_uuid": "workspace", "placement_generation": 1}
    tenant = hashlib.sha256(json.dumps(["instance", "workspace"]).encode()).hexdigest()
    scope = Scope(tenant, "bot", "group:123", "member")
    context = {"scope": scope.as_dict(), "created": 10, "request_key": "synthetic-query", "host_route": route}
    spec = importlib.util.spec_from_file_location("gif_file_tools", ROOT / "custom-plugins/QQTextReader/pkg/file_tools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    store = AssetStore.from_env()
    jobs = JobService(tmp_path / "data")
    stop = asyncio.Event()
    running = asyncio.create_task(jobs.worker().serve(stop))
    try:
        result = await module.animation(None, None, context, scope, store, {"action": "create", "recipe": recipe()})
        assert result["status"] == "completed" and result["delivery_status"] == "queued"
        ref = result["result_refs"][0]
        bot = SimpleNamespace(enable=True, adapter=SimpleNamespace(send_message=AsyncMock()))
        ap = SimpleNamespace(workspace_service=SimpleNamespace(instance_uuid="instance"))
        delivery = HostDelivery(ap, tmp_path / "data", resolver=AsyncMock(return_value=bot))
        assert await delivery.tick()
        chain = bot.adapter.send_message.call_args.args[2]
        assert chain.root[0].type == "Image"
        raw = base64.b64decode(chain.root[0].base64)
        with Image.open(io.BytesIO(raw)) as image:
            assert image.n_frames > 1
        assert not await delivery.tick()
        assert bot.adapter.send_message.await_count == 1
        from langbot.pkg.platform.adapters.aiocqhttp.message_converter import AiocqhttpMessageConverter
        onebot, _, _ = await AiocqhttpMessageConverter.yiri2target(chain)
        assert onebot[0]["type"] == "image"
        assert base64.b64decode(onebot[0]["data"]["file"].removeprefix("base64://")) == raw
        previous = await module.animation(None, None, context, scope, store, {"action": "inspect", "base_ref": ref})
        assert previous["recipe"]["layers"] == recipe()["layers"]
        context["request_key"] = "follow-up"
        context["created"] = 10**12
        revised = await module.animation(None, None, context, scope, store, {
            "action": "revise", "base_ref": ref, "speed": .5, "deliver": False})
        assert revised["status"] == "completed" and revised["result"]["duration_ms"] == 2000
        from qunling.transfers import send_attachment
        api = SimpleNamespace(reply=AsyncMock())
        await send_attachment(store, scope, context, api, revised["result_refs"][0])
        assert api.reply.call_args.args[0].root[0].type == "Image"
    finally:
        stop.set()
        await running
