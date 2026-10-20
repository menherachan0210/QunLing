from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main
from main import (
    AIImagePlugin,
    GeneratedImage,
    ImageGenerationError,
    _image_result,
    image_data_url,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"fixture"
PAYLOAD = base64.b64encode(PNG).decode()
DATA_URL = "data:image/png;base64," + PAYLOAD


def plugin_with_client(monkeypatch, result=None, error=None, config=None):
    client = SimpleNamespace(
        images=SimpleNamespace(
            generate=AsyncMock(return_value=result, side_effect=error)
        ),
        post=AsyncMock(return_value=result, side_effect=error),
        close=AsyncMock(),
    )
    plugin = AIImagePlugin()
    plugin.plugin_runtime_handler = SimpleNamespace()
    plugin.config = {
        "openai_api_key": "test-secret",
        "model_name": "gpt-image-2.5-sunburst",
        **(config or {}),
    }
    monkeypatch.setattr(plugin, "create_client", lambda: client)
    return plugin, client


def response(**kwargs):
    return SimpleNamespace(data=[SimpleNamespace(**kwargs)])


@pytest.mark.asyncio
async def test_generate_uses_exact_model_single_image_and_base64_contract(monkeypatch):
    plugin, client = plugin_with_client(monkeypatch, response(b64_json=PAYLOAD))
    result = await plugin.generate_image(" draw a cat ")
    assert result == GeneratedImage(base64=DATA_URL)
    client.images.generate.assert_awaited_once_with(
        model="gpt-image-2.5-sunburst",
        prompt="draw a cat",
        size="1024x1024",
        quality="auto",
        n=1,
        response_format="b64_json",
    )
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_edit_uses_json_image_data_not_model_supplied_url(monkeypatch):
    plugin, client = plugin_with_client(monkeypatch, response(b64_json=PAYLOAD))
    assert await plugin.edit_image(
        "make it blue", GeneratedImage(base64=DATA_URL)
    ) == GeneratedImage(base64=DATA_URL)
    client.images.generate.assert_not_awaited()
    client.post.assert_awaited_once()
    assert client.post.await_args.args == ("/images/edits",)
    assert client.post.await_args.kwargs["body"] == {
        "model": "gpt-image-2.5-sunburst",
        "prompt": "make it blue",
        "size": "1024x1024",
        "quality": "auto",
        "n": 1,
        "response_format": "b64_json",
        "images": [{"image_url": DATA_URL}],
    }
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_multi_edit_sends_all_ordered_references_in_one_paid_request(monkeypatch):
    plugin, client = plugin_with_client(monkeypatch, response(b64_json=PAYLOAD))
    inputs = [
        GeneratedImage(base64=DATA_URL),
        GeneratedImage(url="https://example.test/second"),
    ]
    normalized = [DATA_URL, "data:image/png;base64,c2Vjb25k"]
    loader = AsyncMock(side_effect=normalized)
    monkeypatch.setattr(main, "image_data_url", loader)
    assert await plugin.edit_image(
        "交换两个角色的衣服并合成一张", inputs
    ) == GeneratedImage(base64=DATA_URL)
    client.post.assert_awaited_once()
    client.images.generate.assert_not_awaited()
    body = client.post.await_args.kwargs["body"]
    assert body["images"] == [{"image_url": value} for value in normalized]
    assert body["n"] == 1
    assert [call.args[0] for call in loader.await_args_list] == inputs


@pytest.mark.asyncio
async def test_any_failed_reference_prevents_entire_upstream_request(monkeypatch):
    plugin, client = plugin_with_client(monkeypatch, response(b64_json=PAYLOAD))
    loader = AsyncMock(
        side_effect=[DATA_URL, ImageGenerationError("参考图片下载失败，未提交")]
    )
    monkeypatch.setattr(main, "image_data_url", loader)
    with pytest.raises(ImageGenerationError, match="未提交"):
        await plugin.edit_image("合成两张", [GeneratedImage(base64=DATA_URL)] * 2)
    assert loader.await_count == 2
    client.post.assert_not_awaited()
    client.images.generate.assert_not_awaited()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sources", [[], [GeneratedImage()] * 5, ["https://example.test/image"], [None]]
)
async def test_invalid_multi_reference_count_or_type_never_creates_client(
    monkeypatch, sources
):
    plugin, client = plugin_with_client(monkeypatch)
    with pytest.raises(ImageGenerationError, match="1–4"):
        await plugin.edit_image("edit", sources)
    client.post.assert_not_awaited()
    client.images.generate.assert_not_awaited()
    client.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_reference_global_download_deadline_prevents_post(monkeypatch):
    plugin, client = plugin_with_client(monkeypatch)

    async def never_finishes(image):
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "image_data_url", never_finishes)
    original_timeout = asyncio.timeout
    monkeypatch.setattr(main.asyncio, "timeout", lambda seconds: original_timeout(0.01))
    with pytest.raises(ImageGenerationError, match="尚未提交"):
        await plugin.edit_image("edit", [GeneratedImage(base64=DATA_URL)] * 2)
    client.post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [TimeoutError("SECRET"), RuntimeError("SECRET https://private.example/?key=x")],
)
async def test_provider_failure_is_safe_and_never_retried(monkeypatch, error):
    plugin, client = plugin_with_client(monkeypatch, error=error)
    with pytest.raises(ImageGenerationError) as caught:
        await plugin.generate_image("cat")
    assert "SECRET" not in str(caught.value)
    assert "private.example" not in str(caught.value)
    assert "未自动重试" in str(caught.value)
    assert client.images.generate.await_count == 1
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_total_deadline_cancels_request_and_closes_client(monkeypatch):
    plugin, client = plugin_with_client(monkeypatch)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def wait_forever(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client.images.generate.side_effect = wait_forever
    original_wait = asyncio.wait_for

    async def fast_wait(awaitable, *, timeout):
        assert timeout == 300
        return await original_wait(awaitable, timeout=0.01)

    monkeypatch.setattr(main.asyncio, "wait_for", fast_wait)
    with pytest.raises(ImageGenerationError, match="超时"):
        await plugin.generate_image("cat")
    assert started.is_set() and cancelled.is_set()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_timeout", [150, 300])
async def test_explicit_request_timeout_is_honored(monkeypatch, configured_timeout):
    plugin, client = plugin_with_client(
        monkeypatch,
        response(b64_json=PAYLOAD),
        config={"request_timeout_seconds": configured_timeout},
    )
    original_wait = asyncio.wait_for

    async def record_wait(awaitable, *, timeout):
        assert timeout == configured_timeout
        return await original_wait(awaitable, timeout=timeout)

    monkeypatch.setattr(main.asyncio, "wait_for", record_wait)
    assert await plugin.generate_image("cat") == GeneratedImage(base64=DATA_URL)
    client.images.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_above_three_hundred_seconds_rejected_before_request(monkeypatch):
    plugin, client = plugin_with_client(
        monkeypatch, config={"request_timeout_seconds": 301}
    )
    with pytest.raises(ImageGenerationError, match="30–300"):
        await plugin.generate_image("cat")
    client.images.generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["", "  ", "x" * 8001, None])
async def test_invalid_prompt_does_not_call_provider(monkeypatch, prompt):
    plugin, client = plugin_with_client(monkeypatch)
    with pytest.raises(ImageGenerationError):
        await plugin.generate_image(prompt)
    client.images.generate.assert_not_awaited()


@pytest.mark.parametrize(
    "item",
    [
        {"b64_json": "not base64"},
        {"b64_json": base64.b64encode(b"not an image").decode()},
        {"url": "file:///private.png"},
        {"url": "https://user:password@example.test/a"},
        {},
    ],
)
def test_invalid_results_are_rejected(item):
    with pytest.raises(ImageGenerationError):
        _image_result(response(**item))


def test_url_only_provider_is_compatible():
    assert _image_result(
        response(url="https://image.example.test/output.png")
    ) == GeneratedImage(url="https://image.example.test/output.png")


def test_oversize_result_rejected_before_decode(monkeypatch):
    monkeypatch.setattr(main, "MAX_IMAGE_BYTES", 4)
    with pytest.raises(ImageGenerationError, match="10 MiB"):
        _image_result(response(b64_json=PAYLOAD))


def test_ten_megabyte_image_fits_runtime_reply_envelope():
    from langbot_plugin.api.entities.builtin.platform.message import Image, MessageChain
    from langbot_plugin.runtime.io.connection import MAX_MESSAGE_BYTES

    assert main.MAX_IMAGE_BYTES == 10 * 1024 * 1024
    raw = PNG + bytes(main.MAX_IMAGE_BYTES - len(PNG))
    result = _image_result(response(b64_json=base64.b64encode(raw).decode()))
    envelope = {
        "action": "reply_message",
        "data": {
            "query_id": 1,
            "query_uuid": "test-query",
            "message_chain": MessageChain([Image(base64=result.base64)]).model_dump(
                mode="json"
            ),
            "quote_origin": False,
        },
    }
    assert len(json.dumps(envelope).encode()) < MAX_MESSAGE_BYTES - 1024 * 1024


def test_one_byte_above_ten_megabytes_is_rejected():
    raw = PNG + bytes(main.MAX_IMAGE_BYTES + 1 - len(PNG))
    with pytest.raises(ImageGenerationError, match="10 MiB"):
        _image_result(response(b64_json=base64.b64encode(raw).decode()))


@pytest.mark.asyncio
async def test_edit_without_source_never_calls_provider(monkeypatch):
    plugin, client = plugin_with_client(monkeypatch)
    with pytest.raises(ImageGenerationError, match="引用"):
        await plugin.edit_image("blue", GeneratedImage())
    client.post.assert_not_awaited()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_reference_url_download_is_bounded_and_has_no_api_auth(monkeypatch):
    from pkg import remote_image

    calls = []

    class Reply:
        headers = {"Content-Length": str(len(PNG))}
        status = 200

        def __init__(self):
            self.content = self

        async def iter_chunked(self, size):
            yield PNG

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["timeout"].total == 20
            assert kwargs["auto_decompress"] is False
            assert "Authorization" not in kwargs["headers"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, *, allow_redirects):
            assert allow_redirects is False
            calls.append(("GET", url))
            return Reply()

    monkeypatch.setattr(remote_image.aiohttp, "TCPConnector", lambda **kwargs: object())
    monkeypatch.setattr(remote_image.aiohttp, "ClientSession", Client)
    assert (
        await image_data_url(GeneratedImage(url="https://image.example.test/signed"))
        == DATA_URL
    )
    assert calls == [("GET", "https://image.example.test/signed")]
