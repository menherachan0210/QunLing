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
        assert db.execute('select sum(weight) from permits where active=1').fetchone()[0] == 128
    await iterator.aclose()
    assert stream.closed
    with sqlite3.connect(tmp_path / 'resource-admission.sqlite3') as db:
        assert db.execute('select count(*) from permits').fetchone()[0] == 0
