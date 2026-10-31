import asyncio
from copy import deepcopy
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from qunling import model_media, resource_gate
from qunling.request_journal import classify


def photo(value):
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + value}}


def test_history_has_one_shared_image_budget_and_keeps_newest_inputs(monkeypatch):
    item = photo("pixels")
    monkeypatch.setattr(model_media, "MAX_MODEL_IMAGE_CHARS", len(item['image_url']['url']) * 2)
    messages = [{"role": "user", "content": [{"type": "text", "text": str(i)}, item]} for i in range(4)]
    saved = deepcopy(messages)
    bounded = model_media.bounded_images(messages)
    assert [m['content'][1]['type'] for m in bounded] == ['text', 'text', 'image_url', 'image_url']
    assert [m['content'][0]['text'] for m in bounded] == ['0', '1', '2', '3']
    assert messages == saved  # Stored conversation and edit references are unchanged.


def test_remote_images_share_count_limit_and_tool_protocol_is_preserved(monkeypatch):
    monkeypatch.setattr(model_media, 'MAX_MODEL_IMAGES', 1)
    messages = [{'role': 'user', 'content': [photo('old')]},
                {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call1'}]},
                {'role': 'tool', 'tool_call_id': 'call1', 'content': 'result'},
                {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'https://example.test/image'}}]}]
    result = model_media.bounded_images(messages)
    assert result[0]['content'][0]['type'] == 'text'
    assert result[1:] == messages[1:]


@pytest.mark.asyncio
async def test_media_is_not_admitted_under_pressure_but_text_still_runs(tmp_path, monkeypatch):
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    original = resource_gate.ResourceGate
    monkeypatch.setattr(resource_gate, 'ResourceGate', lambda root: original(root, sample=lambda: resource_gate.Pressure(2048, 100)))
    async with model_media.model_request_slot([{'role': 'user', 'content': 'hello'}]):
        pass
    with pytest.raises(resource_gate.ResourceBusy) as caught:
        async with asyncio.timeout(7):
            async with model_media.model_request_slot([{'role': 'user', 'content': [photo('x' * 300000)]}]):
                pytest.fail('The provider must not be called under memory pressure')
    assert classify(caught.value) == 'capacity'
    with sqlite3.connect(tmp_path / 'resource-admission.sqlite3') as db:
        assert db.execute('select count(*) from permits').fetchone()[0] == 0


@pytest.mark.sdk
@pytest.mark.asyncio
async def test_closing_real_requester_stream_releases_provider_and_media_lease(tmp_path, monkeypatch):
    from langbot.pkg.provider.modelmgr.requesters import litellmchat
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    original = resource_gate.ResourceGate
    monkeypatch.setattr(resource_gate, 'ResourceGate', lambda root: original(root, sample=lambda: resource_gate.Pressure(2048, 2000)))
    request = litellmchat.LiteLLMRequester(ap=SimpleNamespace(logger=Mock()), config={})
    request._build_completion_args = AsyncMock(return_value={'messages': [{'role': 'user', 'content': [photo('x' * 300000)]}]})
    class Stream:
        closed = False
        def __aiter__(self):
            return self
        async def __anext__(self):
            return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(model_dump=lambda: {'content': 'hi'}), finish_reason=None)])
        async def aclose(self):
            self.closed = True
    stream = Stream()
    monkeypatch.setattr(litellmchat, 'acompletion', AsyncMock(return_value=stream))
    iterator = request.invoke_llm_stream(None, None, [])
    assert (await anext(iterator)).content == 'hi'
    with sqlite3.connect(tmp_path / 'resource-admission.sqlite3') as db:
        assert db.execute('select sum(weight) from permits where active=1').fetchone()[0] == 32
    await iterator.aclose()
    assert stream.closed
    with sqlite3.connect(tmp_path / 'resource-admission.sqlite3') as db:
        assert db.execute('select count(*) from permits').fetchone()[0] == 0


@pytest.mark.parametrize('size,expected', [(351172, 32), (4 * 1024**2, 64), (12 * 1024**2, 128)])
def test_transfer_budget_scales_with_compressed_payload_not_pixel_count(size, expected):
    assert model_media.buffer_mib(size) == expected


@pytest.mark.sdk
@pytest.mark.asyncio
async def test_warm_busy_host_sends_static_screenshot_without_animation_work(tmp_path, monkeypatch):
    import base64
    import io
    from PIL import Image, PngImagePlugin
    from qunling import vision_input
    info = PngImagePlugin.PngInfo()
    info.add_text('fixture', 'x' * 270_000)
    output = io.BytesIO()
    with Image.new('RGB', (2802, 1801), 'black') as screenshot:
        screenshot.save(output, format='PNG', pnginfo=info)
    value = 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode()
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    original = resource_gate.ResourceGate
    sample = lambda: resource_gate.Pressure(1930, 493, some=6.72, full=5.04, local_available_mib=178)
    monkeypatch.setattr(resource_gate, 'ResourceGate', lambda root: original(root, sample=sample))
    blocked = original(tmp_path, sample=sample)
    file_ticket = blocked.ticket('file')
    assert blocked.try_acquire(file_ticket) is None
    decoder = AsyncMock(side_effect=AssertionError('A static screenshot must not start the animation decoder'))
    monkeypatch.setattr(vision_input, '_isolated_animation', decoder)
    content = await vision_input.model_image_content(value)
    assert content.type == 'image_base64' and content.image_base64 == value
    decoder.assert_not_called()
    messages = model_media.bounded_images([{'role': 'user', 'content': [photo(value.partition(',')[2])]}])
    async with model_media.model_request_slot(messages):
        with sqlite3.connect(blocked.path) as db:
            assert db.execute('SELECT sum(weight) FROM permits WHERE active=1').fetchone()[0] == 32
    blocked.release(file_ticket)
