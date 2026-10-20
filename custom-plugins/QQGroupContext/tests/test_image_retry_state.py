"""Exercise real query guards and tool output using local fake downloads/models."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pkg import tools
from pkg.errors import ImageReadError, VisionError
from test_context_tools import fixture, history


def ready(monkeypatch):
    plugin, _, variables, context = fixture()
    context.history = history(context.timestamp)
    tools.present_history(context, 100)
    plugin.invoke_llm = AsyncMock(
        return_value=SimpleNamespace(
            content="两张合成测试图的比较结果。", tool_calls=[]
        )
    )
    api = SimpleNamespace(
        get_query_vars=AsyncMock(return_value=variables),
        get_bot_uuid=AsyncMock(return_value=context.bot),
    )
    plugin.plugin_runtime_handler = None
    monkeypatch.setattr(tools, "QueryBasedAPIProxy", lambda **kw: api)
    return plugin, context


PARAMS = {"image_refs": ["img_1", "img_2"], "question": "比较这两张图。"}
PIXELS = "data:image/jpeg;base64,c3ludGhldGlj"


async def invoke(plugin, params=None):
    return json.loads(
        await tools.execute(
            SimpleNamespace(plugin=plugin), "images", params or PARAMS, 10, "query-a"
        )
    )


@pytest.mark.asyncio
async def test_failed_download_can_retry_once_and_preserves_paid_call_budget(
    monkeypatch,
):
    plugin, context = ready(monkeypatch)
    load = AsyncMock(
        side_effect=[
            PIXELS,
            ImageReadError(
                "synthetic URL secret",
                stage="dns",
                code="dns_non_public",
            ),
            PIXELS,
            PIXELS,
        ]
    )
    monkeypatch.setattr(tools, "load_image", load)
    first = await invoke(plugin)
    assert first["stage"] == "dns"
    assert first["code"] == "dns_non_public"
    assert first["failed_refs"] == ["img_2"]
    assert first["retryable"] is True
    assert first["failures"] == [
        {"image_ref": "img_2", "stage": "dns", "code": "dns_non_public"}
    ]
    assert "secret" not in json.dumps(first)
    assert not context.vision_used
    assert not context.vision_inflight
    plugin.invoke_llm.assert_not_called()
    assert (await invoke(plugin))["status"] == "ok"
    assert context.vision_used
    assert context.image_read_attempts == 2
    plugin.invoke_llm.assert_awaited_once()
    assert (await invoke(plugin))["code"] == "vision_already_used"
    assert load.await_count == 4


@pytest.mark.asyncio
async def test_two_failed_reads_stop_without_calling_or_claiming_vision(monkeypatch):
    plugin, context = ready(monkeypatch)
    load = AsyncMock(side_effect=RuntimeError("https://private.example/?secret=1"))
    monkeypatch.setattr(tools, "load_image", load)
    first, second, third = (
        await invoke(plugin),
        await invoke(plugin),
        await invoke(plugin),
    )
    assert first["retryable"] is True
    assert second["retryable"] is False
    assert second["failed_refs"] == ["img_1", "img_2"]
    assert third["code"] == "image_read_attempts_exhausted"
    assert third["retryable"] is False
    assert "private.example" not in json.dumps([first, second, third])
    assert "secret" not in json.dumps([first, second, third])
    assert load.await_count == 4
    assert not context.vision_used
    plugin.invoke_llm.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["download", "vision"])
async def test_concurrent_calls_cannot_download_or_bill_twice(monkeypatch, phase):
    plugin, context = ready(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delay(*args, **kwargs):
        entered.set()
        await release.wait()
        return (
            PIXELS
            if phase == "download"
            else SimpleNamespace(content="合成结果", tool_calls=[])
        )

    load = AsyncMock(return_value=PIXELS)
    monkeypatch.setattr(tools, "load_image", load)
    if phase == "download":
        load.side_effect = delay
    else:
        plugin.invoke_llm.side_effect = delay
    task = asyncio.create_task(invoke(plugin))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        duplicate = await invoke(plugin)
        assert duplicate["code"] == "image_analysis_in_progress"
        assert duplicate["retryable"] is False
        assert context.image_read_attempts == 1
        assert context.vision_used is (phase == "vision")
    finally:
        release.set()
    assert (await task)["status"] == "ok"
    assert load.await_count == 2
    plugin.invoke_llm.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["download", "vision"])
async def test_cancellation_releases_inflight_but_never_unclaims_upstream(
    monkeypatch, phase
):
    plugin, context = ready(monkeypatch)
    entered = asyncio.Event()

    async def delay(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    load = AsyncMock(return_value=PIXELS)
    monkeypatch.setattr(tools, "load_image", load)
    if phase == "download":
        load.side_effect = delay
    else:
        plugin.invoke_llm.side_effect = delay
    task = asyncio.create_task(invoke(plugin))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not context.vision_inflight
    assert context.vision_used is (phase == "vision")
    load.side_effect = None
    if phase == "download":
        assert (await invoke(plugin))["status"] == "ok"
        plugin.invoke_llm.assert_awaited_once()
        assert context.image_read_attempts == 2
    else:
        assert (await invoke(plugin))["code"] == "vision_already_used"
        plugin.invoke_llm.assert_awaited_once()
        assert context.image_read_attempts == 1


@pytest.mark.asyncio
async def test_download_deadline_cancels_children_and_allows_one_retry(monkeypatch):
    plugin, context = ready(monkeypatch)
    cancelled = []

    async def delay(url):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(tools, "load_image", delay)
    monkeypatch.setattr(tools, "IMAGE_READ_TIMEOUT", 0.005)
    result = await invoke(plugin)
    assert result["stage"] == "download"
    assert result["code"] == "image_download_timeout"
    assert result["failed_refs"] == ["img_1", "img_2"]
    assert result["retryable"] is True
    assert len(cancelled) == 2
    assert not context.vision_used and not context.vision_inflight
    plugin.invoke_llm.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        TimeoutError("private model url"),
        RuntimeError("secret API key"),
        SimpleNamespace(content="", tool_calls=[]),
        SimpleNamespace(content="unexpected tool", tool_calls=[{}]),
    ],
)
async def test_upstream_errors_and_invalid_responses_never_retry(monkeypatch, response):
    plugin, context = ready(monkeypatch)
    monkeypatch.setattr(tools, "load_image", AsyncMock(return_value=PIXELS))
    if isinstance(response, Exception):
        plugin.invoke_llm.side_effect = response
    else:
        plugin.invoke_llm.return_value = response
    first = await invoke(plugin)
    assert first["stage"] == "vision"
    assert first["failed_refs"] == []
    assert first["retryable"] is False
    assert "private" not in json.dumps(first) and "secret" not in json.dumps(first)
    assert context.vision_used and not context.vision_inflight
    assert (await invoke(plugin))["code"] == "vision_already_used"
    plugin.invoke_llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_outer_deadline_after_upstream_started_is_not_a_download_retry(
    monkeypatch,
):
    plugin, context = ready(monkeypatch)
    monkeypatch.setattr(tools, "load_image", AsyncMock(return_value=PIXELS))
    timeout = asyncio.timeout
    outer = []

    def observed_timeout(seconds):
        manager = timeout(seconds)
        if seconds == tools.VISION_TOTAL_TIMEOUT:
            outer.append(manager)
        return manager

    monkeypatch.setattr(tools.asyncio, "timeout", observed_timeout)

    async def delay(**kwargs):
        # Expire only after reaching the upstream boundary, without a short
        # wall-clock race against Windows scheduler granularity.
        assert context.vision_used
        outer[0].reschedule(asyncio.get_running_loop().time() - 1)
        await asyncio.Event().wait()

    plugin.invoke_llm.side_effect = delay
    result = await invoke(plugin)
    assert result["code"] == "vision_timeout"
    assert result["retryable"] is False
    assert context.vision_used and not context.vision_inflight
    assert (await invoke(plugin))["code"] == "vision_already_used"
    plugin.invoke_llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_parameter_and_scope_errors_never_consume_download_budget(monkeypatch):
    plugin, context = ready(monkeypatch)
    load = AsyncMock(return_value=PIXELS)
    monkeypatch.setattr(tools, "load_image", load)
    for refs in [["https://private.example"], ["img_999"], []]:
        with pytest.raises(VisionError):
            await tools.read_images(
                plugin, context, {"image_refs": refs, "question": "合成问题"}
            )
    assert context.image_read_attempts == 0
    assert not context.vision_used and not context.vision_inflight
    load.assert_not_called()
    plugin.invoke_llm.assert_not_called()


def test_unavailable_attachment_hints_do_not_drop_missing_images_or_leak_sources():
    _, _, _, context = fixture()
    context.history = history(context.timestamp)
    context.history["messages"][0]["unavailable_images"] = [
        {"index": 1, "reason": "missing_url", "url": "https://private.example"},
        {"index": 2, "reason": "too_large", "file_id": "synthetic secret"},
        {"index": 3, "reason": "https://private.example"},
        {"index": True, "reason": "missing_url"},
        {"index": -1, "reason": "missing_url"},
        "synthetic secret",
    ]
    result = tools.present_history(context, 100)
    message = result["messages"][0]
    assert message["image_refs"] == ["img_1"]
    assert message["unavailable_image_count"] == 3
    assert message["unavailable_images"] == [
        {"index": 1, "reason": "missing_url"},
        {"index": 2, "reason": "too_large"},
        {"index": 3, "reason": "unavailable"},
    ]
    assert "齐全" in message["image_warning"]
    assert "private.example" not in json.dumps(result)
    assert "synthetic secret" not in json.dumps(result)
    assert context.shown_images == {"img_1", "img_2"}
