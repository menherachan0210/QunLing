import base64
import io
import asyncio
import sqlite3
import sys

import pytest

pytestmark = pytest.mark.sdk
from PIL import Image

from qunling.vision_input import animation_data_url, model_image_content, decoder_memory_mib


def animated_url():
    frames = [Image.new("RGB", (32, 32), color) for color in ("red", "blue", "green")]
    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=[100, 200, 300])
    return "data:image/gif;base64," + base64.b64encode(output.getvalue()).decode()


def test_animation_model_input_contains_chronological_frames_not_raw_gif():
    original = animated_url()
    result, animated = animation_data_url(original)
    assert animated and result.startswith("data:image/png;base64,")
    with Image.open(io.BytesIO(base64.b64decode(result.split(",")[1]))) as sheet:
        colors = sheet.convert("RGB").getdata()
        assert (255, 0, 0) in colors and (0, 0, 255) in colors and (0, 128, 0) in colors
        assert sheet.width == 900
    assert original == animated_url()


@pytest.mark.asyncio
async def test_bad_animation_is_a_placeholder_not_a_failed_chat_turn():
    value = "data:image/gif;base64," + base64.b64encode(b"GIF89a corrupt").decode()
    result = await model_image_content(value)
    assert result.type == "text" and "读取失败" in result.text


@pytest.mark.asyncio
async def test_host_input_accepts_both_plain_and_data_uri_gif():
    original = animated_url()
    for value in [original, original.split(",")[1]]:
        result = await model_image_content(value)
        assert result.type == "image_base64" and result.image_base64.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_large_compressed_canvas_is_rejected_before_unbounded_decode():
    raw = bytearray(base64.b64decode(animated_url().split(',')[1]))
    raw[6:10] = (6000).to_bytes(2, 'little') * 2
    value = 'data:image/gif;base64,' + base64.b64encode(raw).decode()
    result = await model_image_content(value)
    assert result.type == 'text' and '读取失败' in result.text


@pytest.mark.asyncio
async def test_static_picture_bytes_are_preserved():
    output = io.BytesIO()
    with Image.new('RGB', (64, 64), 'red') as image:
        image.save(output, format='PNG')
    value = 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode()
    result = await model_image_content(value)
    assert result.image_base64 == value


@pytest.mark.asyncio
@pytest.mark.parametrize('during_launch', [False, True])
async def test_cancel_kills_and_reaps_decoder_before_releasing_capacity(tmp_path, monkeypatch, during_launch):
    from qunling import vision_input, resource_gate
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('QUNLING_TEST_PROVIDER_SECRET', 'must-not-reach-decoder')
    gate = resource_gate.ResourceGate
    monkeypatch.setattr(resource_gate, 'ResourceGate', lambda root: gate(root, sample=lambda: resource_gate.Pressure(2048, 2000)))
    started, process_created = asyncio.Event(), asyncio.Event()
    create = asyncio.create_subprocess_exec
    children = []
    async def slow_decoder(*args, **kwargs):
        assert 'QUNLING_TEST_PROVIDER_SECRET' not in kwargs['env']
        started.set()
        if during_launch:
            await asyncio.sleep(.1)
        process = await create(sys.executable, '-I', '-c', 'import time; time.sleep(60)', **kwargs)
        children.append(process)
        process_created.set()
        return process
    monkeypatch.setattr(vision_input.asyncio, 'create_subprocess_exec', slow_decoder)
    task = asyncio.create_task(vision_input.model_image_base64(animated_url()))
    await asyncio.wait_for(started.wait() if during_launch else process_created.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert children and children[0].returncode is not None
    with sqlite3.connect(tmp_path / 'resource-admission.sqlite3') as db:
        assert db.execute('select count(*) from permits').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_decoder_deadline_returns_a_placeholder_and_reaps_process(monkeypatch):
    from qunling import vision_input
    create = asyncio.create_subprocess_exec
    children = []
    async def hung_decoder(*args, **kwargs):
        process = await create(sys.executable, '-I', '-c', 'import time; time.sleep(60)', **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(vision_input.asyncio, 'create_subprocess_exec', hung_decoder)
    monkeypatch.setattr(vision_input, 'DECODE_TIMEOUT', .1)
    result = await model_image_content(animated_url())
    assert result.type == 'text' and children[0].returncode is not None


@pytest.mark.parametrize("fmt,animated,lossless", [
    ('GIF', True, False), ('PNG', False, False), ('PNG', True, False),
    ('WEBP', False, False), ('WEBP', False, True), ('WEBP', True, False),
])
@pytest.mark.asyncio
async def test_small_image_formats_decode_with_the_small_budget(fmt, animated, lossless):
    output = io.BytesIO()
    with Image.new('RGB', (201, 203), 'red') as first, Image.new('RGB', (201, 203), 'blue') as last:
        options = {'save_all': True, 'append_images': [last], 'duration': 100} if animated else {}
        if fmt == 'WEBP':
            options['lossless'] = lossless
        first.save(output, format=fmt, **options)
    value = base64.b64encode(output.getvalue()).decode()
    assert decoder_memory_mib(value) == 128
    result = await model_image_content(value)
    assert result.type == 'image_base64'
    if animated:
        assert result.image_base64.startswith('data:image/png;')


def test_canvas_and_encoded_input_both_count_toward_decoder_budget():
    raw = bytearray(base64.b64decode(animated_url().partition(',')[2]))
    raw[6:10] = (2000).to_bytes(2, 'little') * 2
    assert decoder_memory_mib(base64.b64encode(raw).decode()) == 256
    raw[6:10] = (4000).to_bytes(2, 'little') + (3000).to_bytes(2, 'little')
    assert decoder_memory_mib(base64.b64encode(raw).decode()) == 384
    raw[6:10] = (32).to_bytes(2, 'little') * 2
    raw.extend(b'\x00' * (9 * 1024**2))
    assert decoder_memory_mib(base64.b64encode(raw).decode()) == 256
    assert decoder_memory_mib('not a valid image header') == 384


@pytest.mark.asyncio
async def test_small_animation_runs_under_realistic_headroom_and_matching_child_cap(tmp_path, monkeypatch):
    from qunling import resource_gate, vision_input
    original = resource_gate.ResourceGate
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(resource_gate, 'ResourceGate', lambda root: original(root,
        sample=lambda: resource_gate.Pressure(1930, 540, some=.87, full=.75, local_available_mib=312)))
    create = asyncio.create_subprocess_exec
    caps = []
    async def launch(*args, **kwargs):
        caps.append(args[-1])
        return await create(*args, **kwargs)
    monkeypatch.setattr(vision_input.asyncio, 'create_subprocess_exec', launch)
    result = await model_image_content(animated_url())
    assert result.type == 'image_base64' and caps == ['128']
    with sqlite3.connect(tmp_path / 'resource-admission.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM permits').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_capacity_placeholder_and_log_are_distinct_from_a_corrupt_image(monkeypatch, caplog):
    from qunling import resource_gate, vision_input
    async def busy(_):
        raise resource_gate.ResourceBusy('private input must not appear in diagnostics')
    monkeypatch.setattr(vision_input, 'model_image_base64', busy)
    result = await model_image_content(animated_url())
    assert result.type == 'text' and '服务器暂时繁忙' in result.text
    assert '本轮不要重复' in result.text and '不代表图片损坏' in result.text
    assert 'reason=capacity' in caplog.text and 'private input' not in caplog.text
