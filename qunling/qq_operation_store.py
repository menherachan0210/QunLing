"""Bounded QQ event/request records and durable at-most-once action receipts."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager

from qunling.assets import data_root


class OperationStore:
    def __init__(self, path=None, clock=time.time):
        self.path = path or data_root() / "qq-operations.sqlite3"
        self.clock = clock
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("QQ 操作记录路径不可用。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, chat TEXT NOT NULL,
                    request TEXT NOT NULL, operation TEXT NOT NULL, at REAL NOT NULL,
                    status TEXT NOT NULL, receipt TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS action_time ON actions(scope, chat, at);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, chat TEXT NOT NULL,
                    at REAL NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS event_time ON events(scope, chat, at);
            """)
            db.execute("DELETE FROM actions WHERE at < ?", (clock() - 7 * 86400,))
            db.execute("DELETE FROM events WHERE at < ?", (clock() - 86400,))
            db.execute("DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY at DESC LIMIT -1 OFFSET 2000)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, scope, chat, request, operation, arguments):
        key = ([scope, "request", arguments["request_ref"]] if operation == "respond_request"
               else [scope, chat, request, operation, arguments])
        identity = hashlib.sha256(json.dumps(key,
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        now = self.clock()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT status,receipt FROM actions WHERE id=?", (identity,)).fetchone()
            if old:
                return identity, {"status": old["status"], "already_attempted": True,
                                  "receipt": json.loads(old["receipt"])}
            if db.execute("SELECT COUNT(*) FROM actions WHERE scope=? AND chat=? AND request=?", (scope, chat, request)).fetchone()[0] >= 6:
                raise ValueError("本轮 QQ 操作已达到 6 次上限。")
            if db.execute("SELECT COUNT(*) FROM actions WHERE scope=? AND chat=? AND at>?", (scope, chat, now - 60)).fetchone()[0] >= 12:
                raise ValueError("本会话 QQ 操作过于频繁，请稍后。")
            if db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] >= 10000:
                raise ValueError("QQ 操作记录容量已满，暂不接受新操作。")
            db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?)",
                       (identity, scope, chat, request, operation, now, "unconfirmed", "{}"))
        return identity, None

    def finish(self, identity, status, receipt):
        with self.connect() as db:
            db.execute("UPDATE actions SET status=?,receipt=? WHERE id=?",
                       (status, json.dumps(receipt, ensure_ascii=False), identity))

    def event(self, scope, chat, event):
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode()) > 4096:
            return
        ref = "qqevt_" + hashlib.sha256((scope + chat + encoded).encode()).hexdigest()[:32]
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)", (ref, scope, chat, self.clock(), encoded))

    def events(self, scope, chat=None, limit=50):
        with self.connect() as db:
            sql = "SELECT * FROM events WHERE scope=? AND at>?"
            args = [scope, self.clock() - 86400]
            if chat is not None:
                sql += " AND chat=?"
                args.append(chat)
            rows = db.execute(sql + " ORDER BY at DESC LIMIT ?", [*args, limit]).fetchall()
            result = []
            for row in rows:
                item = {"event_ref": row["id"], "chat": row["chat"], "recorded_at": row["at"], **json.loads(row["payload"])}
                if item.get("request"):
                    key = hashlib.sha256(json.dumps([scope, "request", row["id"]], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    action = db.execute("SELECT status FROM actions WHERE id=?", (key,)).fetchone()
                    item["operation_status"] = action[0] if action else "pending"
                result.append(item)
        return result

    def clear_events(self, scope, chat):
        # Action receipts remain to prevent duplicate mutations after memory reset.
        with self.connect() as db:
            db.execute("DELETE FROM events WHERE scope=? AND chat=?", (scope, chat))


def record_native(adapter, payload):
    """Called only after the authenticated OneBot transport accepts an event."""
    runtime = getattr(adapter, "_runtime", None)
    context = getattr(runtime, "execution_context", None)
    if context is None or payload.get("post_type") not in {"notice", "request"}:
        return
    from qunling.qq_group_api import qq_id
    try:
        actor = qq_id(payload.get("user_id"))
        group = qq_id(payload["group_id"]) if payload.get("group_id") else None
        if group:
            from qunling.group_reset import allows
            if not allows(context, str(runtime.bot_entity.uuid), group, payload.get("time")):
                return
        scope = scope_key(context.instance_uuid, context.workspace_uuid, str(runtime.bot_entity.uuid))
        event = {"type": str(payload.get("notice_type") or payload.get("request_type") or "")[:60],
                 "sub_type": str(payload.get("sub_type", ""))[:60], "user_id": actor,
                 "time": payload.get("time", 0) if type(payload.get("time")) in (int, float) else 0,
                 "request": payload["post_type"] == "request"}
        for key in ("target_id", "operator_id", "message_id", "duration", "group_id"):
            value = payload.get(key)
            if type(value) in (str, int):
                event[key] = str(value)[:100]
        for key in ("flag", "comment"):
            value = payload.get(key)
            if isinstance(value, str):
                event[key] = value[:512 if key == "flag" else 200]
        if isinstance(payload.get("likes"), list):
            event["likes"] = [{k: v for k, v in row.items() if k in {"emoji_id", "count"} and type(v) in (str, int)}
                              for row in payload["likes"][:10] if isinstance(row, dict)]
        OperationStore().event(scope, f"group:{group}" if group else f"person:{actor}", event)
    except Exception:
        # Incoming business messages must survive a full/unavailable diagnostic store.
        return


def scope_key(instance, workspace, bot):
    return hashlib.sha256(json.dumps([instance, workspace, bot]).encode()).hexdigest()
