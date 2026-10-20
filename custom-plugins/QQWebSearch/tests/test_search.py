from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from pkg import search
from pkg.errors import WebError


CONFIG = {
    "api_base_url": "https://provider.example/v1/",
    "api_key": "synthetic-secret-never-real",
    "model_name": "test-model",
    "request_timeout_seconds": 30,
}


def citation(url="https://publisher.example/title", title="出版社"):
    return {"type": "url_citation", "url": url, "title": title}


def output_items(*, annotations=None, status="completed", text="已查到中文资料。"):
    return [
        {"type": "web_search_call", "status": status},
        {
            "type": "message",
            "content": [
                {
                    "type": "output_text",
                    "text": text,
                    "annotations": [citation()] if annotations is None else annotations,
                }
            ],
        },
    ]


def event_bytes(event):
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n").encode()


def stream_events(items=None, *, completed=True, final_output=True):
    items = output_items() if items is None else items
    chunks = [b": keepalive\r\n\r\n"]
    for item in items:
        chunks.append(event_bytes({"type": "response.output_item.done", "item": item}))
    if completed:
        response = {"status": "completed"}
        if final_output:
            response["output"] = items
        chunks.append(event_bytes({"type": "response.completed", "response": response}))
    chunks.append(b"data: [DONE]\r\n\r\n")
    return chunks


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks, exception=None):
        self.chunks = chunks
        self.exception = exception

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.exception is not None:
            raise self.exception


def mock_provider(monkeypatch, chunks, *, status=200, exception=None):
    real_client = httpx.AsyncClient
    calls, options = [], []

    async def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://other.example/collect"},
            stream=ByteStream(chunks, exception),
        )

    def factory(**kwargs):
        options.append(kwargs)
        return real_client(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(search.httpx, "AsyncClient", factory)
    return calls, options


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [1, 2, 7, 256, 65536])
async def test_completed_search_handles_fragmented_utf8_and_crlf(
    monkeypatch, chunk_size
):
    wire = b"".join(stream_events())
    chunks = [wire[n : n + chunk_size] for n in range(0, len(wire), chunk_size)]
    calls, options = mock_provider(monkeypatch, chunks)
    result = await search.search_web(CONFIG, "  最新作品资料  ")
    assert result["answer"] == "已查到中文资料。"
    assert result["search_calls"] == 1
    assert result["sources"] == [{
        "url": citation()["url"], "title": "出版社", "domain": "publisher.example",
        "cited": True, "read_status": "not_read",
    }]
    assert "不是指令" in result["instruction"]
    assert len(calls) == 1
    assert str(calls[0].url) == "https://provider.example/v1/responses"
    payload = json.loads(calls[0].content)
    assert payload["input"] == "最新作品资料"
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["tool_choice"] == "required"
    assert "max_tool_calls" not in payload
    assert payload["stream"] is True and payload["store"] is False
    assert "untrusted data" in payload["instructions"]
    assert "api_key" not in payload
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
    assert CONFIG["api_key"] not in json.dumps(result)


@pytest.mark.asyncio
async def test_completed_output_replaces_intermediate_items_without_duplicates(
    monkeypatch,
):
    mock_provider(monkeypatch, stream_events())
    result = await search.search_web(CONFIG, "测试")
    assert result["search_calls"] == 1
    assert result["answer"].count("中文资料") == 1


@pytest.mark.asyncio
async def test_item_events_work_when_completion_omits_output(monkeypatch):
    mock_provider(monkeypatch, stream_events(final_output=False))
    assert (await search.search_web(CONFIG, "测试"))["search_calls"] == 1


@pytest.mark.asyncio
async def test_image_search_requests_existing_images_without_invented_urls(monkeypatch):
    calls, _ = mock_provider(monkeypatch, stream_events())
    await search.search_web(CONFIG, "角色全身", images=True)
    prompt = json.loads(calls[0].content)["instructions"]
    assert "existing pictures" in prompt
    assert "Do not invent image URLs" in prompt
    assert "visually inspected" in prompt


@pytest.mark.parametrize("completed", [False, None])
def test_partial_search_is_never_presented_as_verified(completed):
    with pytest.raises(WebError, match="未完整"):
        search.parse_results(output_items(), completed)


@pytest.mark.parametrize("status", ["in_progress", "failed", None])
def test_citations_without_a_completed_search_are_not_evidence_of_search(status):
    with pytest.raises(WebError, match="真实联网"):
        search.parse_results(output_items(status=status), True)


def test_invented_answer_without_search_record_is_rejected():
    with pytest.raises(WebError, match="真实联网"):
        search.parse_results(output_items()[1:], True)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/x", "file:///secret", "https://user:pass@example.com", None],
)
def test_invalid_citation_cannot_satisfy_source_requirement(url):
    with pytest.raises(WebError, match="公开来源"):
        search.parse_results(output_items(annotations=[citation(url)]), True)


def test_sources_are_unique_bounded_and_answers_are_bounded():
    annotations = [
        citation("https://publisher.example/first", "长" * 300),
        citation("https://publisher.example/first", "重复"),
        *[citation(f"https://publisher.example/{index}") for index in range(10)],
    ]
    result = search.parse_results(
        output_items(annotations=annotations, text="文" * 20000), True
    )
    assert len(result["answer"]) == 10000
    assert len(result["sources"]) == 11
    assert len({item["url"] for item in result["sources"]}) == 11
    assert len(result["sources"][0]["title"]) == 240


def test_section_citations_are_fetchable_and_deduplicated_by_page():
    result = search.parse_results(
        output_items(
            annotations=[
                citation("https://publisher.example/title#chapter-one"),
                citation("https://publisher.example/title#chapter-two"),
            ]
        ),
        True,
    )
    assert result["sources"] == [
        {"url": "https://publisher.example/title", "title": "出版社",
         "domain": "publisher.example", "cited": True, "read_status": "not_read"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "认证"),
        (403, "权限"),
        (429, "限流"),
        (400, "不接受"),
        (404, "不接受"),
        (422, "不接受"),
        (500, "HTTP 500"),
        (302, "HTTP 302"),
    ],
)
async def test_http_failure_is_safe_and_never_retried_or_redirected(
    monkeypatch, status, expected
):
    calls, _ = mock_provider(
        monkeypatch, [b"private-provider-error-secret"], status=status
    )
    with pytest.raises(WebError, match=expected) as exc:
        await search.search_web(CONFIG, "测试")
    assert len(calls) == 1
    assert "private-provider-error-secret" not in str(exc.value)
    assert CONFIG["api_key"] not in str(exc.value)
    assert exc.value.retryable is False
    assert exc.value.code == (
        "search_authentication_failed" if status in {401, 403}
        else "search_rate_or_quota_limit" if status == 429 else "search_service_error"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exception", [httpx.ReadTimeout("private"), TimeoutError("private")]
)
async def test_timeout_is_safe_and_not_retried(monkeypatch, exception):
    calls, _ = mock_provider(monkeypatch, [], exception=exception)
    with pytest.raises(WebError, match="超时") as exc:
        await search.search_web(CONFIG, "测试")
    assert "private" not in str(exc.value)
    assert len(calls) == 1
    assert exc.value.code == "search_timeout" and exc.value.retryable is False


@pytest.mark.asyncio
async def test_cancellation_propagates(monkeypatch):
    calls, _ = mock_provider(monkeypatch, [], exception=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await search.search_web(CONFIG, "测试")
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["response.failed", "response.incomplete", "error"])
async def test_upstream_failure_event_never_leaks_its_body(monkeypatch, kind):
    mock_provider(
        monkeypatch, [event_bytes({"type": kind, "error": "private-api-error"})]
    )
    with pytest.raises(WebError, match="未完成") as exc:
        await search.search_web(CONFIG, "测试")
    assert "private-api-error" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunks",
    [[b"data: invalid private-json\n\n"], [b'data: {"incomplete": '], [b"\xff\n"]],
)
async def test_malformed_stream_fails_safely(monkeypatch, chunks):
    mock_provider(monkeypatch, chunks)
    with pytest.raises(WebError) as exc:
        await search.search_web(CONFIG, "测试")
    assert "private-json" not in str(exc.value)


@pytest.mark.asyncio
async def test_done_marker_does_not_mean_response_completed(monkeypatch):
    mock_provider(monkeypatch, stream_events(completed=False))
    with pytest.raises(WebError, match="未完整"):
        await search.search_web(CONFIG, "测试")


@pytest.mark.asyncio
async def test_stream_budget_stops_reading_even_for_valid_events(monkeypatch):
    monkeypatch.setattr(search, "MAX_STREAM_BYTES", 64)
    mock_provider(monkeypatch, stream_events())
    with pytest.raises(WebError, match="过大"):
        await search.search_web(CONFIG, "测试")


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", " ", "字" * 1001, None, [], True])
async def test_invalid_query_never_calls_provider(monkeypatch, query):
    calls, _ = mock_provider(monkeypatch, stream_events())
    with pytest.raises(WebError):
        await search.search_web(CONFIG, query)
    assert calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("api_key", ""),
        ("api_key", None),
        ("api_base_url", "file:///secret"),
        ("api_base_url", "https://user:pass@example.com"),
        ("api_base_url", "https://example.com?key=secret"),
        ("model_name", ""),
        ("model_name", None),
        ("request_timeout_seconds", True),
        ("request_timeout_seconds", 29),
        ("request_timeout_seconds", 91),
        ("request_timeout_seconds", float("nan")),
    ],
)
def test_invalid_provider_config_rejected_before_request(field, value):
    with pytest.raises(WebError):
        search.config_values({**CONFIG, field: value})
