"""Run durable local processing jobs without model, QQ, or query API access."""
from __future__ import annotations

import argparse
import asyncio
import signal
import os
from pathlib import Path
import time
import tempfile

from qunling.assets import data_root
from qunling.jobs import JobService
from qunling.resource_gate import FileAdmission, ResourceGate


async def serve(*, once=False, concurrency=2):
    heartbeat_path = Path(os.environ.get("QUNLING_WORKER_HEARTBEAT", str(Path(tempfile.gettempdir()) / "qunling-worker-heartbeat")))
    if not once:
        # Docker retains /tmp across a process restart and can reuse the same
        # PID. Never let the previous process's fresh timestamp report ready
        # before this instance has opened and recovered its task store.
        heartbeat_path.unlink(missing_ok=True)
    service = JobService(data_root())
    await asyncio.to_thread(service.prune)
    worker = service.worker(concurrency=concurrency, admission=FileAdmission(ResourceGate(data_root())))
    if once:
        try:
            await worker.run_once()
            await worker.wait_idle()
        finally:
            await worker.close()
        return
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
    async def maintenance():
        next_prune = time.monotonic() + 3600
        while not stop.is_set():
            # A blocked operation cannot fake liveness; this is on the worker's
            # event loop. The container healthcheck also rejects old instances.
            await asyncio.to_thread(heartbeat_path.write_text, str(os.getpid()), encoding="ascii")
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                if time.monotonic() >= next_prune:
                    await asyncio.to_thread(service.prune)
                    next_prune = time.monotonic() + 3600
    cleanup = asyncio.create_task(maintenance())
    execution = asyncio.create_task(worker.serve(stop))
    try:
        done, _ = await asyncio.wait({cleanup, execution}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        stop.set()
        await asyncio.gather(execution, cleanup, return_exceptions=True)
        heartbeat_path.unlink(missing_ok=True)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main():
    parser = argparse.ArgumentParser(description="Run bounded QunLing file jobs; never sends QQ messages.")
    parser.add_argument("--once", action="store_true", help="Recover and run one bounded batch, then exit.")
    parser.add_argument("--concurrency", type=int, choices=range(1, 9), default=2)
    options = parser.parse_args()
    try:
        asyncio.run(serve(once=options.once, concurrency=options.concurrency))
    except KeyboardInterrupt:
        pass
    except Exception:
        # Do not write payloads, paths or raw exception text to process logs.
        parser.exit(1, "QunLing task worker could not start or continue; check private storage and configuration.\n")


if __name__ == "__main__":
    main()
