"""Exercise the packaged SDK fix with Unicode and actual stdio framing."""
import asyncio
import json
from pathlib import Path

import pytest

from test_runtime_patches import patcher


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def chunks(tmp_path):
    target = tmp_path / 'langbot_plugin/runtime/io/connection.py'
    target.parent.mkdir(parents=True)
    target.write_bytes((ROOT / 'tests/fixtures/plugin_runtime/sdk_connection.py.txt').read_bytes())
    for name in ('sdk-attachment-message-budget.patch', 'sdk-utf8-chunk-performance.patch'):
        source = (ROOT / 'patches' / name).read_text(encoding='utf-8')
        plan = patcher.build_plan(name, tmp_path, source, 'sdk')
        assert plan.state == 'ready'
        patcher.execute_plans([plan], apply=True)
        assert patcher.build_plan(name, tmp_path, source, 'sdk').state == 'already-applied'
    scope = {}
    exec(compile(target.read_text(encoding='utf-8'), str(target), 'exec'), scope)
    assert scope['MAX_MESSAGE_BYTES'] == 32 * 1024 * 1024
    assert scope['MAX_MESSAGE_FRAGMENTS'] == 4096
    return scope['split_utf8_chunks']


def reference_split(text, limit):
    result, current = [], ''
    for char in text:
        if len(char.encode()) > limit:
            raise ValueError
        if len((current + char).encode()) > limit:
            result.append(current)
            current = ''
        current += char
    return result + ([current] if current else [])


@pytest.mark.parametrize('text', ['', 'ascii', '中é🙂文', '\x00\né中🙂end', 'a' * 70 + '🙂'])
@pytest.mark.parametrize('limit', [1, 2, 3, 4, 5, 7, 16, 64])
def test_utf8_boundaries_match_original_contract(chunks, text, limit):
    if any(len(char.encode()) > limit for char in text):
        with pytest.raises(ValueError, match='code point'):
            chunks(text, limit)
    else:
        assert chunks(text, limit) == reference_split(text, limit)


@pytest.mark.parametrize('limit', [0, -1])
def test_invalid_budget(chunks, limit):
    with pytest.raises(ValueError, match='positive'):
        chunks('', limit)


def test_large_image_payload_is_byte_identical(chunks):
    text = json.dumps({'text': '多张图片 🙂', 'base64': 'aB09+/' * 500_000}, ensure_ascii=False)
    parts = chunks(text, 16384)
    assert ''.join(parts) == text
    assert all(0 < len(part.encode()) <= 16384 for part in parts)


@pytest.mark.asyncio
async def test_actual_sdk_stdio_roundtrip(chunks, monkeypatch):
    from langbot_plugin.runtime.io.connections import stdio
    monkeypatch.setattr(stdio, 'split_utf8_chunks', chunks)
    reader = asyncio.StreamReader(limit=128 * 1024)

    class Writer:
        def write(self, value):
            reader.feed_data(value)

        async def drain(self):
            await asyncio.sleep(0)

    wire = stdio.StdioConnection(reader, Writer())
    text = json.dumps({'text': '图片 🙂', 'base64': 'ab09+/' * 180_000}, ensure_ascii=False)
    _, received = await asyncio.gather(wire.send(text), wire.receive())
    assert received == text
