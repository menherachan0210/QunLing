import base64
import io
import asyncio
import sqlite3
import sys

import pytest

pytestmark = pytest.mark.sdk
from PIL import Image

from qunling.vision_input import animation_data_url, model_image_content


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
