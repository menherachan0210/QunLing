"""Local readiness checks that need no administrative credentials."""

from __future__ import annotations

import json
import sys
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener


def check(role: str) -> bool:
    if role == "file-worker":
        try:
            path = Path(os.environ.get("QUNLING_WORKER_HEARTBEAT", "/tmp/qunling-worker-heartbeat"))
            pid = int(path.read_text(encoding="ascii"))
            os.kill(pid, 0)
            return 0 <= time.time() - path.stat().st_mtime < 30
        except (OSError, ValueError):
            return False
    port = {"app": 5300, "wizard": 5310}[role]
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
            if response.status != 200:
                return False
            body = json.loads(response.read(64 * 1024))
            if not isinstance(body, dict):
                return False
            if role == "wizard":
                return body.get("status") == "ok"
            resources = body.get("resources")
            runtimes = resources.get("runtimes") if isinstance(resources, dict) else None
            runtime = body.get("plugin_runtime", {})
            return (
                body.get("code") == 0
                and body.get("msg") == "ok"
                and isinstance(runtimes, dict)
                and runtimes.get("plugin_runtime_connected") is True
                and isinstance(runtime, dict)
                and runtime.get("state") == "ready"
                and runtime.get("healthy") is True
            )
    except (HTTPError, OSError, ValueError, TypeError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if check(sys.argv[1]) else 1)
