from __future__ import annotations

import asyncio
import base64
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import aiohttp
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

import main
from main import AIImagePlugin, GeneratedImage, ImageGenerationError
from pkg import remote_image

SECRET = "synthetic-key-prompt-response-body-DO-NOT-PRINT"
SOURCE_URL = f"https://image.example.test/original?token={SECRET}"
REQUEST = httpx.Request("POST", f"https://provider.example.test/v1/{SECRET}")
PNG = b"\x89PNG\r\n\x1a\nfixture"
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


def plugin_and_client(monkeypatch, *, error=None):
    client = SimpleNamespace(
        images=SimpleNamespace(generate=AsyncMock(side_effect=error)),
        post=AsyncMock(side_effect=error),
        close=AsyncMock(),
    )
    plugin = AIImagePlugin()
    plugin.plugin_runtime_handler = SimpleNamespace()
    plugin.config = {"openai_api_key": SECRET}
    monkeypatch.setattr(plugin, "create_client", lambda: client)
    return plugin, client


def provider_status(status, body=None):
    return APIStatusError(
        SECRET,
        response=httpx.Response(
            status,
            request=REQUEST,
            headers={"x-request-id": SECRET},
            json=body if body is not None else {"error": {"message": SECRET}},
        ),
        body=body,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["generate", "edit"])
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (provider_status(401), "认证失败（HTTP 401）"),
        (provider_status(403), "拒绝访问（HTTP 403）"),
        (provider_status(404), "接口或模型不存在（HTTP 404）"),
        (provider_status(413), "过大的请求（HTTP 413）"),
        (provider_status(429), "限流或额度不足（HTTP 429）"),
        (provider_status(400), "不接受本次请求（HTTP 400）"),
        (provider_status(422), "不接受本次请求（HTTP 422）"),
        (provider_status(502), "暂时异常（HTTP 502）"),
        (provider_status(409), "返回 HTTP 409"),
        (APIConnectionError(message=SECRET, request=REQUEST), "服务连接失败"),
        (APITimeoutError(REQUEST), "服务请求超时"),
    ],
)
async def test_upstream_errors_are_distinct_safe_and_not_retried(
    monkeypatch, caplog, operation, error, expected
):
    plugin, client = plugin_and_client(monkeypatch, error=error)
    with pytest.raises(ImageGenerationError) as caught:
        if operation == "edit":
            await plugin.edit_image(SECRET, GeneratedImage(base64=DATA_URL))
        else:
            await plugin.generate_image(SECRET)
    text = str(caught.value)
    assert expected in text
    assert "未自动重试" in text
    assert "参考图片" not in text or "HTTP 413" in text
    assert "未提交" not in text
    rendered = "".join(traceback.format_exception(caught.value))
    assert SECRET not in text + rendered + caplog.text
    assert "provider.example.test" not in text + rendered + caplog.text
    assert client.images.generate.await_count + client.post.await_count == 1
    client.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"code": "content_policy_violation", "message": SECRET},
        {"error": {"type": "content_filter", "message": SECRET}},
        {"error": {"code": " CONTENT_POLICY_VIOLATION ", "message": SECRET}},
    ],
)
async def test_only_explicit_machine_readable_refusal_codes_classify_content(
    monkeypatch, body
):
    plugin, client = plugin_and_client(
        monkeypatch, error=provider_status(400, body=body)
    )
    with pytest.raises(ImageGenerationError, match="内容审核拒绝") as caught:
        await plugin.edit_image(SECRET, GeneratedImage(base64=DATA_URL))
    assert SECRET not in str(caught.value)
    client.post.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        SECRET,
        {"error": "content_policy_violation"},
        {"error": {"message": "content_policy_violation " + SECRET}},
        {"error": {"code": ["content_policy_violation"]}},
        {"error": {"code": "unknown_code", "type": SECRET}},
        {"error": {"code": "content_policy_violation" + SECRET}},
        {"error": {"code": " " * 1000 + "content_policy_violation"}},
    ],
)
async def test_untrusted_error_text_is_not_treated_as_a_content_refusal(
    monkeypatch, body
):
    plugin, _ = plugin_and_client(monkeypatch, error=provider_status(400, body=body))
    with pytest.raises(ImageGenerationError, match="参数兼容性") as caught:
        await plugin.generate_image(SECRET)
    assert "内容审核" not in str(caught.value)
    assert SECRET not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 503])
async def test_service_or_authentication_errors_take_precedence_over_refusal_code(
    monkeypatch, status
):
    plugin, _ = plugin_and_client(
        monkeypatch,
        error=provider_status(status, {"error": {"code": "content_policy_violation"}}),
    )
    with pytest.raises(ImageGenerationError) as caught:
        await plugin.generate_image(SECRET)
    assert f"HTTP {status}" in str(caught.value)
    assert "内容审核" not in str(caught.value)


def mock_reference_download(monkeypatch, *, error=None, raw=PNG, length=None):
    requests = []
    closed = []

    class Reply:
        headers = {} if length is None else {"Content-Length": length}
        status = (
            error.response.status_code
            if isinstance(error, httpx.HTTPStatusError)
            else 200
        )

        def __init__(self):
            self.content = self

        async def iter_chunked(self, size):
            if error is not None and not isinstance(error, httpx.HTTPStatusError):
                raise error
            yield raw

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["auto_decompress"] is False
            assert kwargs["timeout"].total == 20
            assert "Authorization" not in kwargs["headers"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(True)

        def get(self, url, *, allow_redirects):
            assert allow_redirects is False
            requests.append(("GET", url))
            return Reply()

    monkeypatch.setattr(remote_image.aiohttp, "TCPConnector", lambda **kwargs: object())
    monkeypatch.setattr(remote_image.aiohttp, "ClientSession", Client)
    return requests, closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            httpx.HTTPStatusError(
                SECRET,
                request=httpx.Request("GET", SOURCE_URL),
                response=httpx.Response(
                    403,
                    request=httpx.Request("GET", SOURCE_URL),
                    text=SECRET,
                ),
            ),
            "参考图片下载失败（HTTP 403）",
        ),
        (aiohttp.ConnectionTimeoutError(SECRET), "参考图片下载超时"),
        (aiohttp.SocketTimeoutError(SECRET), "参考图片下载超时"),
        (aiohttp.ClientConnectionError(SECRET), "参考图片下载网络失败"),
        (aiohttp.ClientPayloadError(SECRET), "参考图片下载网络失败"),
        (RuntimeError(SECRET), "参考图片下载网络失败"),
    ],
)
async def test_reference_failures_are_not_provider_failures_or_billable_calls(
    monkeypatch, caplog, error, expected
):
    plugin, client = plugin_and_client(monkeypatch)
    requests, closed = mock_reference_download(monkeypatch, error=error)
    with pytest.raises(ImageGenerationError) as caught:
        await plugin.edit_image(SECRET, GeneratedImage(url=SOURCE_URL))
    assert expected in str(caught.value)
    assert "未提交图片编辑请求" in str(caught.value)
    assert "未自动重试" in str(caught.value)
    rendered = "".join(traceback.format_exception(caught.value))
    assert SECRET not in str(caught.value) + rendered + caplog.text
    assert "image.example.test" not in str(caught.value) + rendered + caplog.text
    assert requests == [("GET", SOURCE_URL)] and closed == [True]
    client.post.assert_not_awaited()
    client.images.generate.assert_not_awaited()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "length", "expected"),
    [
        (b"<html>expired signed link</html>", None, "参考图片数据无效"),
        (PNG, "not-a-number", "参考图片下载响应无效"),
        (PNG, "-1", "参考图片下载响应无效"),
        (PNG, str(main.MAX_IMAGE_BYTES + 1), "参考图片超过 10 MiB"),
    ],
)
async def test_bad_reference_response_does_not_blame_generation_service(
    monkeypatch, raw, length, expected
):
    plugin, client = plugin_and_client(monkeypatch)
    mock_reference_download(monkeypatch, raw=raw, length=length)
    with pytest.raises(ImageGenerationError, match=expected) as caught:
        await plugin.edit_image(SECRET, GeneratedImage(url=SOURCE_URL))
    assert "未提交图片编辑请求" in str(caught.value)
    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_reference_total_download_timeout_is_reported_before_provider(
    monkeypatch,
):
    plugin, client = plugin_and_client(monkeypatch)
    original_timeout = asyncio.timeout
    cancelled = asyncio.Event()
    mock_reference_download(monkeypatch)

    async def slow_enter(self):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(remote_image.aiohttp.ClientSession, "__aenter__", slow_enter)

    def short_timeout(seconds):
        assert seconds in {20.0, 25}
        return original_timeout(0.01 if seconds == 20.0 else seconds)

    monkeypatch.setattr(main.asyncio, "timeout", short_timeout)
    with pytest.raises(ImageGenerationError, match="参考图片下载超时"):
        await plugin.edit_image(SECRET, GeneratedImage(url=SOURCE_URL))
    assert cancelled.is_set()
    client.post.assert_not_awaited()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_unexpected_reference_loader_failure_is_safe_and_not_a_provider_failure(
    monkeypatch,
):
    plugin, client = plugin_and_client(monkeypatch)
    monkeypatch.setattr(
        remote_image,
        "fetch_reference_image",
        AsyncMock(side_effect=RuntimeError(SECRET)),
    )
    with pytest.raises(ImageGenerationError, match="参考图片读取失败") as caught:
        await plugin.edit_image(SECRET, GeneratedImage(url=SOURCE_URL))
    assert SECRET not in str(caught.value)
    assert "未提交图片编辑请求" in str(caught.value)
    client.post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "image",
    [
        GeneratedImage(base64="%%%invalid"),
        GeneratedImage(base64="data:image/png;broken"),
        GeneratedImage(base64=base64.b64encode(b"HTML error").decode()),
        GeneratedImage(url="file:///synthetic-secret.png"),
    ],
)
async def test_invalid_reference_inputs_have_safe_reference_specific_errors(
    monkeypatch, image
):
    plugin, client = plugin_and_client(monkeypatch)
    with pytest.raises(ImageGenerationError, match="参考图片") as caught:
        await plugin.edit_image(SECRET, image)
    assert "未提交图片编辑请求" in str(caught.value)
    assert "生成服务" not in str(caught.value)
    client.post.assert_not_awaited()
