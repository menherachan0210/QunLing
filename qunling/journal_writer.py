"""Bounded ordered diagnostic writes outside the Host event loop."""
from __future__ import annotations

import atexit
import queue
import threading
import time
from types import SimpleNamespace
import uuid


class QueuedJournal:
    def __init__(self, path, instance, *, capacity=2048, factory=None):
        from qunling.request_journal import Journal
        self.path, self.instance = path, instance
        self.write_failed = False
        self._queue = queue.Queue(capacity)
        self._factory = factory or Journal
        self._store = None
        self._queue.put_nowait(("initialize", (), {}))
        self._thread = threading.Thread(target=self._run, daemon=True, name="qunling-diagnostics")
        self._thread.start()
        atexit.register(self._exit)

    def _run(self):
        while True:
            method, args, kwargs = self._queue.get()
            try:
                if method == "barrier":
                    args[0].set()
                    continue
                if self._store is None:
                    self._store = self._factory(self.path, self.instance)
                if method == "initialize":
                    continue
                getattr(self._store, method)(*args, **kwargs)
            except Exception:
                self.write_failed = True
            finally:
                self._queue.task_done()

    def _submit(self, method, args, kwargs):
        try:
            self._queue.put_nowait((method, args, kwargs))
        except queue.Full:
            self.write_failed = True

    def start(self, query, owner, *, origin="mention"):
        from qunling.request_journal import _id
        request_id = _id(query.query_uuid) if query.query_uuid else "admission-" + uuid.uuid4().hex
        message_id = getattr(query.message_event, "message_id", None)
        # Only primitive metadata crosses into the queue, never the live Query,
        # messages, credentials or provider error strings.
        snapshot = SimpleNamespace(query_uuid=request_id, message_event=SimpleNamespace(
            message_id=None if message_id is None else _id(message_id)))
        owner = tuple(_id(part) for part in owner)
        self._submit("start", (snapshot, owner), {"origin": origin, "at": time.time()})
        return request_id

    def record(self, request_id, phase, **values):
        from qunling.request_journal import PHASES, REASONS, STATUSES, _id, _tool
        if phase not in PHASES or values.get("status", "active") not in STATUSES or values.get("reason", "") not in REASONS:
            raise ValueError("Invalid diagnostic code")
        if "tool" in values:
            values["tool"] = _tool(values["tool"])
        if "details" in values:
            from qunling.activity import details
            values["details"] = details(values["details"])
        self._submit("record", (_id(request_id), phase), {**values, "at": time.time()})

    def flush(self, timeout=2):
        done = threading.Event()
        try:
            self._queue.put(("barrier", (done,), {}), timeout=timeout)
        except queue.Full:
            self.write_failed = True
            return False
        return done.wait(timeout)

    def listing(self, *args, **kwargs):
        if not self.flush():
            self.write_failed = True
            raise OSError("Diagnostic writer busy")
        if self._store is None:
            raise OSError("Diagnostic storage unavailable")
        return self._store.listing(*args, **kwargs)

    def _exit(self):
        self.flush(timeout=1)
