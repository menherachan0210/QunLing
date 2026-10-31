"""Exercise the real Host response conversion without sending network requests."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from langbot.pkg.plugin.handler import _resolve_remove_think
from langbot.pkg.provider.modelmgr.requesters import litellmchat


def query(remove_think):
    return SimpleNamespace(pipeline_config={
        "ai": {"runner": {"id": "active"}, "runner_config": {
            "active": {"remove-think": remove_think},
            "inactive": {"remove-think": not remove_think},
        }},
        "output": {"misc": {"remove-think": not remove_think}},
    })


@pytest.mark.parametrize("enabled", [True, False])
def test_plugin_calls_inherit_the_active_runner_visibility(enabled):
    assert _resolve_remove_think({}, query(enabled)) is enabled


@pytest.mark.parametrize("explicit", [True, False])
def test_explicit_caller_visibility_still_takes_precedence(explicit):
    assert _resolve_remove_think({"remove_think": explicit}, query(not explicit)) is explicit


def test_legacy_queries_and_unbound_calls_remain_supported():
    old = SimpleNamespace(pipeline_config={"output": {"misc": {"remove-think": True}}})
    assert _resolve_remove_think({}, old) is True
    assert _resolve_remove_think({}, None) is False


ANSWER = "答案是 ABC。\n\n1. 保留中文、**加粗**和链接：https://example.com/a?q=1\n```python\na = 1 < 2\n```"
SUMMARY = "Synthetic internal summary, not a user-facing answer."


def requester():
    instance = litellmchat.LiteLLMRequester(ap=SimpleNamespace(logger=Mock()), config={})
    instance._build_completion_args = AsyncMock(return_value={
        "messages": [{"role": "user", "content": "Synthetic question"}],
        "reasoning_effort": "high",
    })
    return instance


@pytest.mark.asyncio
async def test_nonstream_hides_separate_summary_and_preserves_answer_and_provider_metadata(monkeypatch):
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(model_dump=lambda: {
        "role": "assistant", "content": ANSWER, "reasoning_content": SUMMARY,
    }))])
    provider = AsyncMock(return_value=response)
    monkeypatch.setattr(litellmchat, "acompletion", provider)
    result, _usage = await requester().invoke_llm(None, None, [], remove_think=True)
    assert result.content == ANSWER
    assert result.provider_specific_fields["reasoning_content"] == SUMMARY
    assert provider.await_args.kwargs["reasoning_effort"] == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize("inline", [False, True])
async def test_stream_hides_reasoning_even_when_tags_cross_chunks(monkeypatch, inline):
    deltas = ([{"content": part} for part in ("<thi", "nk>" + SUMMARY, "</th", "ink>")]
              if inline else [{"reasoning_content": SUMMARY[:18]}, {"reasoning_content": SUMMARY[18:]}])
    deltas.extend({"content": part} for part in (ANSWER[:13], ANSWER[13:]))

    class Stream:
        closed = False

        def __aiter__(self):
            self.pending = iter([*deltas, {}])
            return self

        async def __anext__(self):
            try:
                delta = next(self.pending)
            except StopIteration:
                raise StopAsyncIteration
            return SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(model_dump=lambda: delta),
                finish_reason="stop" if not delta else None,
            )])

        async def aclose(self):
            self.closed = True

    stream = Stream()
    provider = AsyncMock(return_value=stream)
    monkeypatch.setattr(litellmchat, "acompletion", provider)
    chunks = [chunk async for chunk in requester().invoke_llm_stream(None, None, [], remove_think=True)]
    assert "".join(chunk.content or "" for chunk in chunks) == ANSWER
    assert stream.closed
    assert provider.await_args.kwargs["reasoning_effort"] == "high"
    if not inline:
        assert "".join((chunk.provider_specific_fields or {}).get("reasoning_content", "") for chunk in chunks) == SUMMARY
