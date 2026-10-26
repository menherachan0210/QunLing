"""Durable group memory boundaries shared by the host and plugin workers.

No chat content is stored here. A failed purge stays fenced until an operator
retries it; completed request IDs make a lost HTTP response safe to retry.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import re
import sqlite3
import time
import uuid

from qunling.assets import data_root
from qunling.group_knowledge import workspace_id


def scope(context, bot, group):
    workspace = workspace_id(getattr(context, "instance_uuid", ""),
                             getattr(context, "workspace_uuid", ""))
    if not workspace or not isinstance(bot, str) or not bot or not re.fullmatch(r"[1-9][0-9]{0,19}", str(group)):
        raise ValueError("Invalid group memory scope")
    return workspace, bot, str(group)


@contextmanager
def connect():
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "group-memory-resets.sqlite3"
    if path.is_symlink():
        raise ValueError("Invalid reset storage")
    db = sqlite3.connect(path, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS boundaries (
            workspace TEXT, bot TEXT, group_id TEXT, generation TEXT NOT NULL,
            cutoff REAL NOT NULL, pending INTEGER NOT NULL,
            PRIMARY KEY(workspace,bot,group_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS receipts (
            workspace TEXT, bot TEXT, group_id TEXT, request_id TEXT,
            cutoff REAL NOT NULL, PRIMARY KEY(workspace,bot,group_id,request_id))""")
        yield db
    finally:
        db.close()


def boundary(key):
    # Standalone plugins and test fixtures may not have the optional host store.
    if not os.environ.get("QUNLING_DATA_DIR"):
        return None
    path = data_root() / "group-memory-resets.sqlite3"
    if not path.exists():
        return None
    with connect() as db:
        row = db.execute("SELECT * FROM boundaries WHERE workspace=? AND bot=? AND group_id=?", key).fetchone()
    return dict(row) if row else None


def for_context(context, bot, group):
    if not os.environ.get("QUNLING_DATA_DIR") or context is None:
        return None
    if not (data_root() / "group-memory-resets.sqlite3").exists():
        return None
    return boundary(scope(context, bot, group))


def allows(context, bot, group, timestamp):
    state = for_context(context, bot, group)
    if state is None:
        return True
    return (not state["pending"] and type(timestamp) in (int, float)
            and timestamp > state["cutoff"])


def event_timestamp(event):
    """The host observes EBA events; the query pool receives legacy events."""
    for name in ("timestamp", "time"):
        value = getattr(event, name, None)
        if type(value) in (int, float) and value > 0:
            return value
    return 0


def pending_groups(context, bot):
    if not os.environ.get("QUNLING_DATA_DIR") or not (data_root() / "group-memory-resets.sqlite3").exists():
        return []
    workspace, bot, _ = scope(context, bot, "1")
    with connect() as db:
        return [row[0] for row in db.execute(
            "SELECT group_id FROM boundaries WHERE workspace=? AND bot=? AND pending=1 ORDER BY group_id", (workspace, bot))]


def completed(key, request_id):
    with connect() as db:
        row = db.execute("SELECT cutoff FROM receipts WHERE workspace=? AND bot=? AND group_id=? AND request_id=?",
                         (*key, request_id)).fetchone()
    return row[0] if row else None


def begin(key):
    with connect() as db, db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT * FROM boundaries WHERE workspace=? AND bot=? AND group_id=?", key).fetchone()
        if previous and previous["pending"]:
            return dict(previous)
        db.execute("INSERT OR REPLACE INTO boundaries VALUES(?,?,?,?,?,1)",
                   (*key, uuid.uuid4().hex, time.time()))
        return dict(db.execute("SELECT * FROM boundaries WHERE workspace=? AND bot=? AND group_id=?", key).fetchone())


def finish(key, request_id):
    with connect() as db, db:
        db.execute("BEGIN IMMEDIATE")
        # Exclude messages received during the purge too, including a delayed
        # same-second QQ event after the HTTP response.
        cutoff = time.time()
        db.execute("UPDATE boundaries SET pending=0,cutoff=? WHERE workspace=? AND bot=? AND group_id=?", (cutoff, *key))
        db.execute("INSERT INTO receipts VALUES(?,?,?,?,?)", (*key, request_id, cutoff))
    return cutoff


def filter_history(runtime, group, result):
    state = for_context(runtime.execution_context, str(runtime.bot_entity.uuid), group)
    if state is None:
        return result
    if state["pending"]:
        raise RuntimeError("Group memory reset is pending")
    result = dict(result)
    result["messages"] = [dict(row) for row in result.get("messages", [])
                          if type(row.get("time")) in (int, float) and row["time"] > state["cutoff"]]
    retained = {row.get("message_id") for row in result["messages"]}
    for row in result["messages"]:
        if row.get("reply_message_id") not in retained:
            row.pop("reply_message_id", None)
    result["memory_reset"] = True
    return result
