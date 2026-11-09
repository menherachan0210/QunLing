import asyncio
import json
import threading
import time

import pytest

from bootstrap.api import SetupError
from bootstrap import capabilities as module

CONFIG = {"base_url": "https://provider.example/v1", "api_key": "synthetic-private-key",
          "chat_model": "chat", "vision_model": "vision", "image_model": "image", "search_model": "search"}


def test_probes_require_explicit_selection_cost_ack_and_do_not_replay():
    release = threading.Event()
    calls = []
    async def test(config, kind):
        calls.append(kind)
        await asyncio.to_thread(release.wait, 2)
        if kind == "vision":
            raise TimeoutError("synthetic-private-key")
        return "done"
    checks = module.Checks(test)
    body = {"checks": ["chat", "vision"], "acknowledge_cost": True, "request_id": "a"*32}
    with pytest.raises(SetupError):
        checks.start(CONFIG, {**body, "acknowledge_cost": False})
    assert not calls
    first = checks.start(CONFIG, body)
    second = checks.start(CONFIG, body)
    assert first["request_id"] == second["request_id"]
    with pytest.raises(SetupError):
        checks.start(CONFIG, {**body, "request_id": "b"*32})
    release.set()
    deadline = time.monotonic() + 3
    while checks.snapshot()["job"]["status"] == "running" and time.monotonic() < deadline:
        time.sleep(.01)
    result = checks.snapshot()
    assert calls == ["chat", "vision"]
    assert result["job"]["results"][1]["status"] == "unconfirmed"
    assert "synthetic-private-key" not in json.dumps(result)
    checks.start(CONFIG, body)
    assert calls == ["chat", "vision"]


@pytest.mark.asyncio
async def test_chat_requires_actual_tool_call_and_vision_actual_image(monkeypatch):
    payloads = []
    async def post(config, path, payload, **kwargs):
        payloads.append(payload)
        if payload["model"] == "vision":
            return {"choices": [{"message": {"content": "红色"}}]}
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "qunling_probe", "arguments": '{"ok":true}'}}]}}]}
    monkeypatch.setattr(module, "_post", post)
    assert "工具" in await module.probe(CONFIG, "chat")
    assert "红色" in await module.probe(CONFIG, "vision")
    assert payloads[0]["tool_choice"]["function"]["name"] == "qunling_probe"
    assert payloads[1]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    async def wrong(*args, **kwargs):
        return {"choices": [{"message": {"content": "I support tools"}}]}
    monkeypatch.setattr(module, "_post", wrong)
    with pytest.raises(module.ProbeError):
        await module.probe(CONFIG, "chat")


@pytest.mark.asyncio
async def test_image_probe_checks_real_bytes_without_saving_or_sending(monkeypatch):
    async def post(*args, **kwargs):
        return {"data": [{"b64_json": module._red_image().split(",", 1)[1]}]}
    monkeypatch.setattr(module, "_post", post)
    assert "未发送" in await module.probe(CONFIG, "image")


@pytest.mark.asyncio
@pytest.mark.usefixtures('available_file_resources')
async def test_native_gif_probe_validates_frames_and_uses_image_provider(monkeypatch):
    import base64, io
    from PIL import Image
    frames = [Image.new('RGB', (32, 32), (i*12, 20, 100)) for i in range(16)]
    stream = io.BytesIO()
    frames[0].save(stream, format='GIF', save_all=True, append_images=frames[1:], duration=200, loop=0)
    for frame in frames:
        frame.close()
    calls = []
    async def post(config, path, payload, **kwargs):
        assert config['base_url'] == 'https://image.example/v1'
        assert config['api_key'] == 'synthetic-image-key'
        assert path == '/images/generations' and kwargs['timeout_seconds'] == 180
        calls.append(payload)
        return {'data': [{'b64_json': base64.b64encode(stream.getvalue()).decode()}]}
    monkeypatch.setattr(module, '_post', post)
    result = await module.probe({**CONFIG, 'image_base_url': 'https://image.example/v1',
        'image_api_key': 'synthetic-image-key', 'image_timeout_seconds': 180}, 'gif')
    assert '16 帧' in result and '3.2 秒' in result and '未发送到 QQ' in result
    assert len(calls) == 1 and calls[0]['frame_count'] == 16 and calls[0]['output_format'] == 'gif'


@pytest.mark.asyncio
@pytest.mark.usefixtures('available_file_resources')
async def test_static_response_cannot_pass_native_gif_probe(monkeypatch):
    calls = []
    async def post(*args, **kwargs):
        calls.append(True)
        return {'data': [{'b64_json': module._red_image().split(',', 1)[1]}]}
    monkeypatch.setattr(module, '_post', post)
    with pytest.raises(ValueError):
        await module.probe(CONFIG, 'gif')
    assert len(calls) == 1
