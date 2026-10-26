"""Bounded operational facts shared by operator diagnostics and self inspection."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

OPERATIONS = frozenset({"list", "import", "inspect", "read", "search", "extract", "create",
    "delete", "status", "cancel", "send", "download", "images", "text", "start", "resume"})


def details(value):
    """Drop everything outside the deliberately small diagnostic schema."""
    if not isinstance(value, dict):
        return {}
    result = {}
    enums = {"kind": {"tool", "participation"}, "operation": OPERATIONS,
        "cache": {"hit", "miss", "unknown", "deduplicated"},
        "outcome": {"ok", "error", "unchanged", "unconfirmed"},
        "action": {"silence", "wait", "reply", "followup", "tool_followup", "research", "task_request"},
        "role": {"addressed", "continuing", "joining", "observing"}}
    for key, allowed in enums.items():
        if isinstance(value.get(key), str) and value[key] in allowed:
            result[key] = value[key]
    for key in ("call_id", "arguments_digest", "target_message_id"):
        if isinstance(value.get(key), str) and re.fullmatch(r"[a-zA-Z0-9_.:@-]{1,160}", value[key]):
            result[key] = value[key]
    for key in ("elapsed_ms", "downloaded_bytes", "items"):
        number = value.get(key)
        if type(number) in (int, float) and math.isfinite(number) and 0 <= number <= 1e10:
            result[key] = round(number, 1)
    if type(value.get("context_sufficient")) is bool:
        result["context_sufficient"] = value["context_sufficient"]
    for key in ("context_message_ids", "information_needs"):
        items = value.get(key)
        if isinstance(items, (list, tuple)):
            result[key] = [v for v in items[:24] if isinstance(v, str)
                           and re.fullmatch(r"[a-zA-Z0-9_.:@-]{1,160}", v)]
    # A routing summary is not a transcript or a complete model prompt.
    for key in ("purpose", "entry_point"):
        text = value.get(key)
        if isinstance(text, str):
            text = re.sub(r"https?://\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]+", "[已隐藏]", text)
            text = re.sub(r"(?i)(?:token|password|secret|api[_ -]?key|密码|密钥)\s*[:=：]\s*\S+", "[已隐藏]", text)
            result[key] = "".join(c for c in text if ord(c) >= 32 and not 0xD800 <= ord(c) <= 0xDFFF)[:160]
    return result


def tool_start(name, parameters, request_id):
    parameters = parameters if isinstance(parameters, dict) else {}
    operation = parameters.get("operation", parameters.get("action", ""))
    if name == "browse_group_files":
        operation = parameters.get("action", "list")
    raw = json.dumps(parameters, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return details({"kind": "tool", "operation": operation,
        "arguments_digest": hashlib.sha256((str(request_id) + raw).encode()).hexdigest()[:24]})


def tool_finish(result, started):
    try:
        value = json.loads(result) if isinstance(result, str) and len(result) <= 2_000_000 else result
    except (ValueError, TypeError):
        value = None
    value = value if isinstance(value, dict) else {}
    io = value.get("io", {})
    io = io if isinstance(io, dict) else {}
    status = value.get("status")
    outcomes = {"error": "error", "failed": "error", "cancelled": "error", "canceled": "error",
        "ok": "ok", "success": "ok", "completed": "ok", "done": "ok", "ready": "ok", "unchanged": "unchanged"}
    outcome = outcomes.get(status, "unconfirmed") if isinstance(status, str) else "unconfirmed"
    return details({"elapsed_ms": (time.monotonic() - started) * 1000,
        "outcome": outcome,
        "cache": io.get("cache", "unknown"), "downloaded_bytes": io.get("downloaded_bytes"),
        "items": io.get("items")})


def read_activity(instance, workspace, bot, group, *, minutes=60, limit=20, now=None):
    """Read only this binding's group facts; never initialize or prune a journal."""
    if any(not isinstance(v, str) or not re.fullmatch(r"[a-zA-Z0-9_.:@-]{1,160}", v)
           for v in (instance, workspace, bot, group)):
        raise ValueError("Invalid activity scope")
    if type(minutes) is not int or not 1 <= minutes <= 1440 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Invalid activity window")
    now = time.time() if now is None else now
    root = os.environ.get("QUNLING_DATA_DIR", "")
    if not root or not Path(root).is_absolute():
        return {"available": False, "operations": []}
    key = hashlib.sha256(instance.encode()).hexdigest()[:32]
    path = Path(root) / "requests" / (key + ".sqlite3")
    if not path.is_file() or path.is_symlink():
        return {"available": False, "operations": []}
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.15)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT created,updated,status,events,origin FROM requests WHERE "
            "instance=? AND workspace=? AND bot=? AND group_id=? AND updated>=? AND origin<>'participation' "
            "ORDER BY created DESC LIMIT 201", (instance, workspace, bot, group, now-minutes*60)).fetchall()
    finally:
        db.close()
    operations, legacy = [], False
    for row in rows[:200]:
        events = json.loads(row["events"])
        finished = {}
        for event in events:
            info = details(event.get("details"))
            if info.get("kind") == "tool" and info.get("call_id"):
                finished[info["call_id"]] = {"at": event["at"], "tool": event.get("tool", ""),
                    **{key: info[key] for key in ("operation", "outcome", "cache", "elapsed_ms", "downloaded_bytes") if key in info}}
            if event.get("phase") == "delivered":
                finished["reply_delivery"] = {"at": event["at"], "tool": "send_reply", "operation": "send", "outcome": "ok"}
        if any(event.get("phase") == "tool_wait" for event in events) and not any(
                event.get("details", {}).get("kind") == "tool" for event in events):
            legacy = True
        operations.extend(finished.values())
    operations.sort(key=lambda item: item["at"], reverse=True)
    return {"available": True, "sampled_at": now, "window_minutes": minutes,
        "operations": operations[:limit], "truncated": len(rows) > 200 or len(operations) > limit,
        "legacy_records_without_operation_detail": legacy,
        "instruction": "这里只能确认本机器人本群已记录的操作；异步写入可能稍有延迟，旧记录、截断或缺失不能证明从未执行。"
                       "cache=hit/deduplicated 不代表重新下载；缺少 outcome 的操作尚未确认完成。"}
