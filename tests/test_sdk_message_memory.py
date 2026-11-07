"""Completed RPC frames must not pin image payloads in an idle receive loop."""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import weakref

import pytest

from test_runtime_patches import patcher


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def handler_module(tmp_path):
    import langbot_plugin

    relative = "langbot_plugin/runtime/io/handler.py"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    shutil.copyfile(Path(langbot_plugin.__file__).parent.parent / relative, target)
    name = "sdk-message-memory.patch"
    source = (ROOT / "patches" / name).read_text(encoding="utf-8")
    plan = patcher.build_plan(name, tmp_path, source, "sdk")
    patcher.execute_plans([plan], apply=True)
    assert patcher.build_plan(name, tmp_path, source, "sdk").state == "already-applied"
    spec = importlib.util.spec_from_file_location("message_memory_handler", target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TrackedText(str):
    pass


class TrackedDict(dict):
    pass


class Pipe:
    def __init__(self):
        self.frames = asyncio.Queue()
        self.idle = asyncio.Event()
        self.sent = asyncio.Event()

    async def receive(self):
        self.idle.set()
        return await self.frames.get()

    async def send(self, value):
        self.sent.set()

    async def close(self):
        pass


async def wait_idle(pipe):
    await asyncio.wait_for(pipe.idle.wait(), 2)
    # Let the action's done callbacks run too; no additional frame is sent.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"seq_id": 1, "action": "synthetic", "data": {"blob": "image"}},
    {"seq_id": 1, "code": 0, "message": "success", "data": {"blob": "image"}},
    {"seq_id": 1, "unrecognized": "image"},
    "invalid-json",
])
async def test_completed_or_ignored_frames_are_released_while_idle(handler_module, tmp_path, payload, monkeypatch):
    # pytest's captured LogRecord would retain JSONDecodeError.doc itself.
    monkeypatch.setattr(handler_module.logger, "warning", lambda *args, **kwargs: None)
    pipe = Pipe()
    handler = handler_module.Handler(pipe, file_storage_dir=tmp_path / "transfers")
    decoded_refs = []

    async def decode(value):
        result = TrackedDict(json.loads(value))
        decoded_refs.append(weakref.ref(result))
        return result

    async def consume(data):
        assert data == {"blob": "image"}
        return handler_module.ActionResponse.success({})

    handler._decode_message = decode
    handler.actions["synthetic"] = consume
    loop = asyncio.create_task(handler.run())
    try:
        await wait_idle(pipe)
        pipe.idle.clear()
        wire = TrackedText(payload if isinstance(payload, str) else json.dumps(payload))
        wire_ref = weakref.ref(wire)
        await pipe.frames.put(wire)
        del wire
        await wait_idle(pipe)
        if isinstance(payload, dict) and "action" in payload:
            await asyncio.wait_for(pipe.sent.wait(), 2)
            await asyncio.sleep(0)
        assert wire_ref() is None
        assert all(reference() is None for reference in decoded_refs)
    finally:
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop


@pytest.mark.asyncio
async def test_inflight_action_keeps_its_payload_until_completion(handler_module, tmp_path):
    pipe = Pipe()
    handler = handler_module.Handler(pipe, file_storage_dir=tmp_path / "transfers")
    entered, finish = asyncio.Event(), asyncio.Event()
    references = []

    async def decode(value):
        result = json.loads(value)
        result["data"] = TrackedDict(result["data"])
        references.append(weakref.ref(result["data"]))
        return result

    async def consume(data):
        entered.set()
        await finish.wait()
        assert data["blob"] == "image"
        return handler_module.ActionResponse.success({})

    handler._decode_message = decode
    handler.actions["synthetic"] = consume
    loop = asyncio.create_task(handler.run())
    try:
        await wait_idle(pipe)
        pipe.idle.clear()
        await pipe.frames.put(json.dumps({"seq_id": 1, "action": "synthetic", "data": {"blob": "image"}}))
        await asyncio.wait_for(entered.wait(), 2)
        await wait_idle(pipe)
        assert references[0]() is not None
        finish.set()
        await asyncio.wait_for(pipe.sent.wait(), 2)
        await asyncio.sleep(0)
        assert references[0]() is None
    finally:
        finish.set()
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop


@pytest.mark.asyncio
async def test_unconsumed_response_still_owns_its_payload(handler_module, tmp_path):
    pipe = Pipe()
    handler = handler_module.Handler(pipe, file_storage_dir=tmp_path / "transfers")
    response = asyncio.get_running_loop().create_future()
    handler.resp_waiters[71] = response
    loop = asyncio.create_task(handler.run())
    try:
        await wait_idle(pipe)
        pipe.idle.clear()
        await pipe.frames.put(json.dumps({"seq_id": 71, "code": 0, "message": "success",
                                         "data": {"blob": "intact-image"}}))
        await wait_idle(pipe)
        assert response.done()
        assert response.result().data == {"blob": "intact-image"}
    finally:
        handler.resp_waiters.pop(71, None)
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop
