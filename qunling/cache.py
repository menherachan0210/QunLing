"""Bounded, disposable JSON cache. Authorization is always performed by callers."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import time


class JsonCache:
    def __init__(self, path, *, ttl=3600, max_bytes=64 * 1024 * 1024,
                 max_items=512, max_item_bytes=4 * 1024 * 1024, clock=time.time):
        self.path = Path(path)
        self.ttl, self.max_bytes = ttl, max_bytes
        self.max_items, self.max_item_bytes, self.clock = max_items, max_item_bytes, clock

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=0.1)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS cache (scope TEXT, key TEXT, value BLOB, "
                   "created REAL, expires REAL, PRIMARY KEY(scope,key))")
        return db

    def get(self, scope, key):
        try:
            with closing(self._connect()) as db, db:
                now = self.clock()
                db.execute("DELETE FROM cache WHERE expires<=?", (now,))
                row = db.execute("SELECT value FROM cache WHERE scope=? AND key=? AND expires>?",
                                 (scope, key, now)).fetchone()
            if row and len(row[0]) <= self.max_item_bytes:
                return json.loads(row[0])
        except (OSError, sqlite3.Error, ValueError, TypeError):
            pass
        return None

    def put(self, scope, key, value):
        try:
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            if len(raw) > min(self.max_item_bytes, self.max_bytes):
                return False
            now = self.clock()
            with closing(self._connect()) as db, db:
                db.execute("DELETE FROM cache WHERE expires<=?", (now,))
                db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?)",
                           (scope, key, raw, now, now + self.ttl))
                rows = db.execute("SELECT scope,key,length(value) FROM cache ORDER BY created DESC,rowid DESC").fetchall()
                total = 0
                for index, (owner, entry, size) in enumerate(rows):
                    total += size
                    if index >= self.max_items or total > self.max_bytes:
                        db.execute("DELETE FROM cache WHERE scope=? AND key=?", (owner, entry))
            return True
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return False

    def prune(self):
        if not self.path.exists():
            return
        try:
            with closing(self._connect()) as db, db:
                db.execute("DELETE FROM cache WHERE expires<=?", (self.clock(),))
        except (OSError, sqlite3.Error):
            pass
