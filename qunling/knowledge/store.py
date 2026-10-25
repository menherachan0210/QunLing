"""SQLite evidence with scope, expiry, quotas and revision-fenced collector leases."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time

from .config import ConfigError, bot_key, defaults, public_scope, validate

MAX_BYTES = 64 * 1024 * 1024
MAX_ITEMS = 1000


def terms(text, *, max_chars=4000, max_terms=128):
    value = str(text)[:max_chars].casefold()
    words = re.findall(r"[a-z0-9_]{2,32}", value)
    for sequence in re.findall(r"[\u3400-\u9fff]{2,}", value):
        words.extend(sequence[i:i+2] for i in range(len(sequence) - 1))
    return set(list(dict.fromkeys(words))[:max_terms])


class Store:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock

    @classmethod
    def from_env(cls):
        from qunling.assets import data_root
        return cls(data_root() / "web-knowledge.sqlite3")

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=0.2)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS evidence (
                    scope TEXT, key TEXT, kind TEXT, body TEXT, terms TEXT,
                    fetched REAL, expires REAL, PRIMARY KEY(scope,key));
                CREATE INDEX IF NOT EXISTS evidence_expiry ON evidence(expires);
                CREATE TABLE IF NOT EXISTS settings (bot TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS sources (
                    bot TEXT, key TEXT, revision INTEGER, token TEXT, lease REAL,
                    next_at REAL, checked REAL, status TEXT, PRIMARY KEY(bot,key));
                CREATE TABLE IF NOT EXISTS budget (bot TEXT PRIMARY KEY, day INTEGER, count INTEGER);
                CREATE TABLE IF NOT EXISTS counters (bot TEXT, name TEXT, value INTEGER,
                    PRIMARY KEY(bot,name));
            """)
            with db:
                yield db
        finally:
            db.close()

    def settings(self, scope, db=None):
        if db is None:
            with self.db() as connection:
                return self.settings(scope, connection)
        row = db.execute("SELECT value FROM settings WHERE bot=?", (bot_key(scope),)).fetchone()
        return validate(json.loads(row[0])) if row else defaults()

    def save(self, scope, value, known_groups):
        value = validate(value)
        if any(source["group_id"] not in known_groups for source in value["sources"]):
            raise ConfigError("只能订阅机器人当前所在群的公开资料。")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.settings(scope, db)
            if value["revision"] != current["revision"]:
                raise ConfigError("设置已变化，请刷新后再保存。")
            value["revision"] += 1
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",
                       (bot_key(scope), json.dumps(value)))
            db.execute("DELETE FROM sources WHERE bot=?", (bot_key(scope),))
            # Remove prior subscriptions immediately; user-derived evidence is
            # separate and is never promoted to a group-wide source.
            for source in current["sources"]:
                from qunling.scope import Scope
                owner = Scope(scope.workspace_id, scope.bot_id, "group:" + source["group_id"], "*")
                db.execute("DELETE FROM evidence WHERE scope=?", (owner.key,))
        return value

    def count(self, scope, name, db=None):
        if name not in {"hit", "miss", "live", "collected", "failed"}:
            return
        if db is None:
            with self.db() as connection:
                return self.count(scope, name, connection)
        db.execute("INSERT INTO counters VALUES (?,?,1) ON CONFLICT(bot,name) "
                   "DO UPDATE SET value=value+1", (bot_key(scope), name))

    def _put(self, db, scope, key, kind, body, ttl):
        # Query refs and read ranges are deliberately not part of persisted evidence.
        body = {k: v for k, v in body.items() if not k.startswith("_") and k not in {
            "page_ref", "search_ref", "read_status", "research_progress", "cache"}}
        text = body.get("full_text", body.get("text", body.get("answer", "")))
        body["content_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False)
        if len(raw.encode()) > 768 * 1024:
            return False
        indexed = " ".join(sorted(terms(body.get("title", "") + " " + text, max_chars=128000, max_terms=4096)))
        now = self.clock()
        db.execute("DELETE FROM evidence WHERE expires<=?", (now,))
        db.execute("INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?,?)",
                   (scope.key, key, kind, raw, indexed, now, now + ttl))
        rows = db.execute("SELECT scope,key,length(CAST(body AS BLOB))+length(CAST(terms AS BLOB)) AS size FROM evidence "
                          "ORDER BY fetched DESC,rowid DESC").fetchall()
        total = 0
        for index, row in enumerate(rows):
            total += row["size"]
            if index >= MAX_ITEMS or total > MAX_BYTES:
                db.execute("DELETE FROM evidence WHERE scope=? AND key=?", (row["scope"], row["key"]))
        return True

    def prune(self):
        with self.db() as db:
            db.execute("DELETE FROM evidence WHERE expires<=?", (self.clock(),))

    def put(self, scope, key, kind, body):
        with self.db() as db:
            settings = self.settings(scope, db)
            if not settings["enabled"]:
                return False
            return self._put(db, scope, key, kind, body, settings["search_ttl" if kind == "summary" else "page_ttl"])

    @staticmethod
    def result(row):
        return {**json.loads(row["body"]), "cache": {"hit": True, "kind": row["kind"],
                "fetched_at": row["fetched"], "expires_at": row["expires"],
                "instruction": "这是此前保存的资料，不代表本轮重新联网核实；仅用于有来源支持的回答。"}}

    def get(self, scope, key):
        with self.db() as db:
            if not self.settings(scope, db)["enabled"]:
                return None
            row = db.execute("SELECT * FROM evidence WHERE scope IN (?,?) AND key=? AND expires>? "
                             "ORDER BY fetched DESC LIMIT 1",
                             (scope.key, public_scope(scope).key, key, self.clock())).fetchone()
            self.count(scope, "hit" if row else "miss", db)
            return self.result(row) if row else None

    def recall(self, scope, query, limit=3):
        needles = terms(query)
        if not needles:
            return []
        with self.db() as db:
            if not self.settings(scope, db)["enabled"]:
                return []
            rows = db.execute("SELECT * FROM evidence WHERE scope IN (?,?) AND kind='page' AND expires>? "
                              "ORDER BY fetched DESC LIMIT 128",
                              (scope.key, public_scope(scope).key, self.clock())).fetchall()
            ranked = []
            for row in rows:
                matches = needles & set(row["terms"].split())
                if len(matches) >= min(2, len(needles)):
                    ranked.append((len(matches) / len(needles), row))
            ranked.sort(key=lambda item: (item[0], item[1]["fetched"]), reverse=True)
            result = []
            for score, row in ranked[:limit]:
                page = self.result(row)
                text = page.get("full_text", page.get("text", ""))
                positions = [text.casefold().find(word) for word in needles]
                start = max(0, min((p for p in positions if p >= 0), default=0) - 250)
                result.append({"url": page["url"], "title": page.get("title", ""),
                               "text": text[start:start+2400], "cache": page["cache"],
                               "published_at": page.get("published_at"), "content_sha256": page.get("content_sha256"),
                               "excerpt": True, "score": round(score, 2)})
            self.count(scope, "hit" if result else "miss", db)
            return result

    @staticmethod
    def source_key(source):
        return hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()

    def claim(self, scope, source, revision):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            config = self.settings(scope, db)
            if not config["enabled"] or config["revision"] != revision or source not in config["sources"]:
                return None
            now, bot, key = self.clock(), bot_key(scope), self.source_key(source)
            row = db.execute("SELECT * FROM sources WHERE bot=? AND key=?", (bot, key)).fetchone()
            if row and (row["next_at"] > now or row["lease"] > now):
                return None
            day = int(now // 86400)
            budget = db.execute("SELECT day,count FROM budget WHERE bot=?", (bot,)).fetchone()
            used = budget["count"] if budget and budget["day"] == day else 0
            if used >= config["daily_budget"]:
                return None
            token = secrets.token_hex(16)
            db.execute("INSERT OR REPLACE INTO budget VALUES (?,?,?)", (bot, day, used+1))
            db.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?,?)",
                       (bot, key, revision, token, now+60, now+source["interval"], now, "running"))
            return token

    def complete(self, scope, source, revision, token, page=None, error="fetch_failed"):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            config = self.settings(scope, db)
            row = db.execute("SELECT * FROM sources WHERE bot=? AND key=?",
                             (bot_key(scope), self.source_key(source))).fetchone()
            if (not row or row["token"] != token or row["lease"] <= self.clock()
                    or not config["enabled"] or config["revision"] != revision):
                return False
            ok = page is not None and page.get("quality") == "readable"
            if ok:
                ok = self._put(db, public_scope(scope), "page:" + source["url"], "page", page, source["ttl"])
                if not ok:
                    error = "content_limit"
            db.execute("UPDATE sources SET status=?,lease=0,token='' WHERE bot=? AND key=?",
                       ("ok" if ok else str(error)[:60], bot_key(scope), self.source_key(source)))
            self.count(scope, "collected" if ok else "failed", db)
            return ok

    def status(self, scope):
        with self.db() as db:
            configured = {self.source_key(source): source for source in self.settings(scope, db)["sources"]}
            sources = {}
            for row in db.execute("SELECT * FROM sources WHERE bot=?", (bot_key(scope),)):
                value = {k: row[k] for k in ("checked", "next_at", "status")}
                if row["status"] == "running" and row["lease"] <= self.clock():
                    value["status"] = "interrupted"
                sources[row["key"]] = value
            for key, value in sources.items():
                value.update(configured.get(key, {}))
            counters = {row["name"]: row["value"] for row in db.execute(
                "SELECT name,value FROM counters WHERE bot=?", (bot_key(scope),))}
            return {"sources": sources, "counters": counters}
