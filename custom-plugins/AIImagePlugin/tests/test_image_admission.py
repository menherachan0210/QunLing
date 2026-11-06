import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from openai import AsyncOpenAI

from main import GeneratedImage, ImageGenerationError, image_transfer_kind
from pkg import image_http
from qunling import resource_gate, vision_input
from test_image_core import DATA_URL, PAYLOAD, plugin_with_client, response


@pytest.mark.asyncio
async def test_small_static_edit_survives_incident_pressure_without_pixel_decode(monkeypatch, tmp_path):
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(resource_gate, 'pressure', lambda: resource_gate.Pressure(
        1930, 502, some=15.97, full=13.3, local_available_mib=141))
    # ResourceGate's default argument is bound at definition time.
    gate_type = resource_gate.ResourceGate
    monkeypatch.setattr(resource_gate, 'ResourceGate', lambda root: gate_type(root, sample=resource_gate.pressure))
    async def forbidden_decode(*args, **kwargs):
        raise AssertionError('A static image must not run a decoder')
    monkeypatch.setattr(vision_input, '_isolated_animation', forbidden_decode)
    plugin, client = plugin_with_client(monkeypatch, response(b64_json=PAYLOAD))
    assert await plugin.edit_image('restyle', GeneratedImage(base64=DATA_URL)) == GeneratedImage(base64=DATA_URL)
    client.post.assert_awaited_once()
    assert client.post.await_args.kwargs['body']['images'] == [{'image_url': DATA_URL}]


@pytest.mark.asyncio
async def test_no_paid_request_when_download_admission_fails(monkeypatch):
    @asynccontextmanager
    async def denied(*args, **kwargs):
        raise resource_gate.ResourceBusy()
        yield
    monkeypatch.setattr(resource_gate, 'resource_slot', denied)
    plugin, client = plugin_with_client(monkeypatch)
    with pytest.raises(ImageGenerationError, match='尚未提交'):
        await plugin.edit_image('restyle', GeneratedImage(base64=DATA_URL))
    client.post.assert_not_awaited()
    client.close.assert_awaited_once()


def test_large_inputs_keep_larger_reservations():
    assert image_transfer_kind(500_000) == 'image_transfer'
    assert image_transfer_kind(8 * 1024**2) == 'image'
    assert image_transfer_kind(40 * 1024**2) == 'image_large'


@pytest.mark.asyncio
async def test_real_sdk_receives_bounded_json_and_sends_only_once():
    calls = []
    async def handler(request):
        calls.append(request)
        assert request.headers['accept-encoding'] == 'identity'
        return httpx.Response(200, json={'created': 1, 'data': [{'b64_json': PAYLOAD}]})
    http = image_http.ImageHTTPClient(transport=httpx.MockTransport(handler))
    async with AsyncOpenAI(api_key='test', base_url='https://image.example/v1',
                          max_retries=0, http_client=http) as client:
        result = await client.images.generate(model='test', prompt='test', n=1)
        assert result.data[0].b64_json == PAYLOAD
    assert len(calls) == 1 and http.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize('headers', [{}, {'content-length': '1'}, {'content-length': '10000'},
                                    {'content-encoding': 'gzip'}])
async def test_oversized_or_compressed_response_is_stopped_and_closed(monkeypatch, headers):
    monkeypatch.setattr(image_http, 'MAX_RESPONSE_BYTES', 128)
    class Stream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            for _ in range(10):
                yield b'x' * 100
        async def aclose(self):
            self.closed = True
    stream = Stream()
    async def handler(request):
        return httpx.Response(200, stream=stream, headers=headers)
    async with image_http.ImageHTTPClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(image_http.ImageResponseLimitError):
            await client.get('https://image.example/v1/images/generations')
    assert stream.closed


@pytest.mark.asyncio
async def test_cancelled_response_does_not_leave_network_open():
    started = asyncio.Event()
    class Stream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b''
        async def aclose(self):
            self.closed = True
    stream = Stream()
    async with image_http.ImageHTTPClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream))) as client:
        task = asyncio.create_task(client.get('https://image.example/v1/images/generations'))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.closed


@pytest.mark.asyncio
async def test_sdk_response_limit_is_reported_without_replay(monkeypatch):
    from main import AIImagePlugin
    monkeypatch.setattr(image_http, 'MAX_RESPONSE_BYTES', 128)
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b'x' * 1024)
    http = image_http.ImageHTTPClient(transport=httpx.MockTransport(handler))
    client = AsyncOpenAI(api_key='test', base_url='https://image.example/v1',
                         max_retries=0, http_client=http)
    plugin = AIImagePlugin()
    plugin.plugin_runtime_handler = object()
    plugin.config = {'model_name': 'test'}
    monkeypatch.setattr(plugin, 'create_client', lambda: client)
    with pytest.raises(ImageGenerationError, match='已停止读取，未自动重试'):
        await plugin.generate_image('test')
    assert len(calls) == 1 and http.is_closed
