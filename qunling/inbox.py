"""Persist bounded QQ file metadata; download bytes only when requested."""
from __future__ import annotations

import json
import secrets
import time

from qunling.assets import AssetError, AssetStore, MAX_ASSET_BYTES, safe_name
from qunling.scope import Scope

RETENTION = 24 * 3600


class Inbox:
    def __init__(self, store: AssetStore):
        self.store = store
        with store._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS inbox(ref TEXT PRIMARY KEY,scope TEXT NOT NULL,"
                "identity TEXT NOT NULL,data TEXT NOT NULL,created REAL NOT NULL,asset_ref TEXT)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS inbox_scope ON inbox(scope,created)")

    def add(self, scope: Scope, data: dict) -> str:
        safe_name(data.get("name"))
        if type(data.get("size", 0)) is not int or not 0 <= data.get("size", 0) <= MAX_ASSET_BYTES:
            raise AssetError("size_limit", "附件超过 40 MiB 处理上限。")
        for key, limit in (("file_id", 4096), ("url", 8192), ("asset_ref", 64)):
            value = data.get(key, "")
            if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
                raise AssetError("invalid_attachment", "附件信息无效。")
        if not any(data.get(k) for k in ("file_id", "url", "asset_ref")):
            raise AssetError("invalid_attachment", "附件没有可读取的内容。")
        # Identity includes the name; a reused filename is never mistaken for
        # a prior native file ID. Metadata is private, not model-facing.
        identity = json.dumps([data.get("file_id"), data.get("url"), data.get("asset_ref"), data["name"]])
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM inbox WHERE created<?", (time.time() - RETENTION,))
            found = db.execute(
                "SELECT ref FROM inbox WHERE scope=? AND identity=?", (scope.key, identity)
            ).fetchone()
            if found:
                return found[0]
            if db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] >= 2048:
                db.execute("DELETE FROM inbox WHERE ref=(SELECT ref FROM inbox ORDER BY created LIMIT 1)")
            ref = "incoming_" + secrets.token_hex(16)
            db.execute("INSERT INTO inbox VALUES(?,?,?,?,?,?)", (
                ref, scope.key, identity, json.dumps(data), time.time(), data.get("asset_ref"),
            ))
        return ref

    def list(self, scope: Scope, *, before=None, limit=30, include_refs=()) -> list[dict]:
        upper = before if before is not None else time.time() + 1
        include_refs = tuple(include_refs[:20])
        placeholders = ",".join("?" for _ in include_refs) or "NULL"
        with self.store._connect() as db:
            rows = db.execute(
                "SELECT ref,data,created,asset_ref FROM inbox WHERE scope=? AND created>? "
                f"AND (created<=? OR ref IN ({placeholders})) "
                f"ORDER BY (ref IN ({placeholders})) DESC,created DESC LIMIT ?",
                (scope.key, time.time() - RETENTION, upper, *include_refs, *include_refs,
                 min(100, max(1, limit))),
            ).fetchall()
        return [{
            "incoming_ref": row["ref"], "name": json.loads(row["data"])["name"],
            "size": json.loads(row["data"]).get("size", 0),
            "received_at": row["created"], "asset_ref": row["asset_ref"],
        } for row in rows]

    def get(self, scope: Scope, ref: str, *, before=None, include_refs=()) -> dict:
        if not isinstance(ref, str) or len(ref) > 64:
            raise AssetError("not_found", "附件编号无效。")
        with self.store._connect() as db:
            row = db.execute(
                "SELECT data,asset_ref FROM inbox WHERE scope=? AND ref=? AND created>? AND created<=?",
                (scope.key, ref, time.time() - RETENTION,
                 before if before is not None and ref not in include_refs else time.time() + 1),
            ).fetchone()
        if row is None:
            raise AssetError("not_found", "附件已过期或不属于当前会话。")
        return {**json.loads(row["data"]), "asset_ref": row["asset_ref"]}

    def attach(self, scope: Scope, ref: str, asset_ref: str):
        self.store.get(scope, asset_ref)
        with self.store._connect() as db:
            db.execute("UPDATE inbox SET asset_ref=? WHERE ref=? AND scope=?", (asset_ref, ref, scope.key))
