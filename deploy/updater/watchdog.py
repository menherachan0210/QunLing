"""Recover a persistently unhealthy component, never replay a chat or restart QQ.

Docker's exit restart policy and the Runtime's worker reconnection remain the
first line of recovery. This is the last resort for an alive but stalled service.
"""
from datetime import datetime
import time

from deploy.updater.locking import worker_lock


def poll(engine, *, clock=time.time):
    from deploy.updater.engine import read, write, TERMINAL
    with worker_lock(engine.root) as acquired:
        if not acquired or read(engine.state_path, {}).get("phase", "idle") not in TERMINAL:
            return
        config = read(engine.active_path, {})
        services = config.get("services", {})
        path = engine.root / "recovery.json"
        records = read(path, {})
        now = clock()
        for service in ("file-worker", "wizard", "app"):
            if service not in services:
                continue
            try:
                info = engine.inspect_service(service)
                labels = info.get("Config", {}).get("Labels", {})
                if (labels.get("com.docker.compose.project") != engine.project
                        or labels.get("com.docker.compose.service") != service):
                    continue
                state = info.get("State", {})
                key = info.get("Id")
                if not key or state.get("Paused") or state.get("Status") != "running":
                    continue
                record = records.setdefault(service, {})
                history = [t for t in record.get("restarts", []) if now - t < 3600]
                record["restarts"] = history
                generation = (key, state.get("StartedAt"))
                if record.get("generation") != list(generation):
                    record.update(generation=list(generation), bad_since=None)
                if state.get("Health", {}).get("Status") != "unhealthy":
                    record["bad_since"] = None
                    continue
                started = datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00")).timestamp()
                if record.get("bad_since") is None:
                    record["bad_since"] = now
                grace = 300 if service == "app" else 120
                cooldown = min(2400, 600 * 2 ** max(0, len(history) - 1))
                if (now - started < grace or now - record["bad_since"] < 90 or len(history) >= 3
                        or (history and now - history[-1] < cooldown)):
                    continue
                # Persist BEFORE the action; a monitor crash cannot cause a
                # restart storm. Restart the exact inspected container only.
                record.update(bad_since=None, restarts=history + [now], last_action="restart", last_at=now)
                write(path, records)
                try:
                    engine.run("docker", "restart", "--time", "30", key, timeout=75)
                except Exception:
                    pass  # The action may have started even if its response failed.
                break  # Never restart multiple components in the same pass.
            except Exception:
                # Observation failures are not evidence that a service is dead.
                continue
        write(path, records)
