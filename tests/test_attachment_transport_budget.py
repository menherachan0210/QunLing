"""Exercise real SDK encoders/receivers over in-memory pipes; never send to QQ."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from langbot_plugin.api.entities.builtin.platform.message import File, MessageChain
from langbot_plugin.entities.io.errors import ConnectionClosedError
from langbot_plugin.runtime.io import connection
from langbot_plugin.runtime.io.connections import stdio, ws
from langbot_plugin.runtime.io.controllers.ws import client, server


MIB = 1024 * 1024


@pytest.fixture(scope="module")
def attachment_message():
    raw = b"synthetic-font!!" * (20 * MIB // 16)  # exactly 20 MiB, no real file
    assert len(raw) == 20 * MIB
    chain = MessageChain(
        [
            File(
                name="synthetic.otf",
                size=len(raw),
                base64=base64.b64encode(raw).decode(),
            )
        ]
    )
    del raw
    message = json.dumps(
        {
            "action": "reply_message",
            "data": {
                "query_id": 1,
                "query_uuid": "synthetic-query",
                "quote_origin": False,
                "message_chain": chain.model_dump(),
            },
        }
    )
    assert 16 * MIB < len(message.encode()) < 32 * MIB
    return message


class PipeWriter:
    def __init__(self, reader):
        self.reader = reader
        self.frames = 0
        self.closed = False

    def write(self, data):
        self.frames += 1
        self.reader.feed_data(data)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True
        self.reader.feed_eof()

    async def wait_closed(self):
        pass


class LoopbackWebSocket:
    """Only wire I/O is fake; SDK UTF-8 splitting and size checks stay real."""

    def __init__(self):
        self.frames = []
        self.closed = False

    async def send(self, data, **kwargs):
        self.frames = [data] if isinstance(data, str) else list(data)

    async def recv_streaming(self, *, decode):
        assert decode is True
        for frame in self.frames:
            yield frame

    async def close(self):
        self.closed = True


def test_all_transport_entry_points_share_finite_32_mib_budget():
    assert connection.MAX_MESSAGE_BYTES == 32 * MIB
    assert connection.MAX_MESSAGE_FRAGMENTS == 4096
    for module in (stdio, ws, client, server):
        assert module.MAX_MESSAGE_BYTES == connection.MAX_MESSAGE_BYTES


@pytest.mark.asyncio
async def test_twenty_mib_attachment_round_trips_real_stdio(
    attachment_message, monkeypatch
):
    # Remove the transport's 1 ms pacing only; keep actual fragmentation, JSON
    # envelopes, byte offsets, UTF-8 encoding, reading and reconstruction.
    original_sleep = asyncio.sleep

    async def cooperative_sleep(_delay):
        await original_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", cooperative_sleep)
    reader = asyncio.StreamReader(limit=128 * 1024)
    writer = PipeWriter(reader)
    sender = stdio.StdioConnection(reader, writer)
    receiver = stdio.StdioConnection(reader, writer)
    _, received = await asyncio.wait_for(
        asyncio.gather(sender.send(attachment_message), receiver.receive()),
        timeout=60,
    )
    assert received == attachment_message
    assert 1024 < writer.frames < connection.MAX_MESSAGE_FRAGMENTS
    await sender.close()


@pytest.mark.asyncio
async def test_twenty_mib_attachment_round_trips_real_websocket(attachment_message):
    wire = LoopbackWebSocket()
    sender = ws.WebSocketConnection(wire)
    receiver = ws.WebSocketConnection(wire)
    await sender.send(attachment_message)
    assert 256 < len(wire.frames) < connection.MAX_MESSAGE_FRAGMENTS
    assert max(len(frame.encode()) for frame in wire.frames) <= 64 * 1024
    assert await receiver.receive() == attachment_message


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "websocket"])
async def test_sender_rejects_over_32_mib_before_writing(transport):
    if transport == "stdio":
        writer = PipeWriter(asyncio.StreamReader())
        transport_connection = stdio.StdioConnection(writer.reader, writer)
    else:
        writer = LoopbackWebSocket()
        transport_connection = ws.WebSocketConnection(writer)
    with pytest.raises(ValueError, match="33554432 byte limit"):
        await transport_connection.send("x" * (32 * MIB + 1))
    assert not writer.frames


@pytest.mark.asyncio
async def test_stdio_receiver_rejects_oversize_header_before_body(caplog):
    reader = asyncio.StreamReader()
    writer = PipeWriter(reader)
    reader.feed_data(
        json.dumps({"type": "chunk_start", "total_size": 32 * MIB + 1}).encode() + b"\n"
    )
    transport = stdio.StdioConnection(reader, writer)
    with pytest.raises(ConnectionClosedError):
        await asyncio.wait_for(transport.receive(), timeout=2)
    assert "Invalid runtime chunked message size" in caplog.text


@pytest.mark.asyncio
async def test_websocket_receiver_rejects_actual_oversize_body_and_closes():
    wire = LoopbackWebSocket()
    # More than the byte budget, yet below the independent fragment budget.
    wire.frames = ["x" * (64 * 1024)] * 512 + ["x"]
    transport = ws.WebSocketConnection(wire)
    with pytest.raises(ConnectionClosedError, match="33554432 byte limit"):
        await transport.receive()
    assert wire.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "websocket"])
async def test_fragment_budget_remains_enforced(transport, caplog):
    if transport == "stdio":
        reader = asyncio.StreamReader(limit=1024 * 1024)
        writer = PipeWriter(reader)
        reader.feed_data(b'{"type":"chunk_start","total_size":1}\n')
        reader.feed_data(
            b'{"type":"chunk_data","offset":0,"data":""}\n'
            * (connection.MAX_MESSAGE_FRAGMENTS + 1)
        )
        transport_connection = stdio.StdioConnection(reader, writer)
        with pytest.raises(ConnectionClosedError):
            await transport_connection.receive()
        assert "too many stdio fragments" in caplog.text
    else:
        wire = LoopbackWebSocket()
        wire.frames = [""] * (connection.MAX_MESSAGE_FRAGMENTS + 1)
        transport_connection = ws.WebSocketConnection(wire)
        with pytest.raises(ConnectionClosedError, match="too many WebSocket fragments"):
            await transport_connection.receive()
        assert wire.closed
