"""Persistent, scoped attachment references shared by installed QunLing tools."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import base64
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from qunling.scope import Scope

MAX_ASSET_BYTES = 40 * 1024 * 1024
MAX_STORE_BYTES = 512 * 1024 * 1024
MAX_ASSETS = 2048
RETENTION_SECONDS = 7 * 86400
REF = re.compile(r"asset_[0-9a-f]{32}")
HASH = re.compile(r"[0-9a-f]{64}")


class AssetError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def data_root() -> Path:
    value = os.environ.get("QUNLING_DATA_DIR")
    if not value:
        raise AssetError("storage_unconfigured", "文件服务尚未配置持久目录，请检查部署。")
    root = Path(value)
    if not root.is_absolute():
        raise AssetError("storage_unconfigured", "文件服务需要绝对数据目录。")
    return root


def safe_name(value: str) -> str:
    if not isinstance(value, str):
        raise AssetError("invalid_name", "文件名无效。")
    value = value.strip()
    if (
        not 1 <= len(value) <= 200
        or value in {".", ".."}
        or any(ord(c) < 32 or c in '/\\:\x7f' for c in value)
        or any(c in '<>"|?*' for c in value)
        or value.endswith(".")
        or value.split(".", 1)[0].lower() in {
            "con", "prn", "aux", "nul",
            *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
        }
    ):
        raise AssetError("invalid_name", "请使用不含路径和控制字符的文件名。")
    return value


def image_name(raw: bytes) -> str:
    """QQ image messages lack filenames; assign a stable name from actual bytes."""
    extension = "bin"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = "png"
    elif raw.startswith(b"\xff\xd8\xff"):
        extension = "jpg"
    elif raw.startswith((b"GIF87a", b"GIF89a")):
        extension = "gif"
    elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        extension = "webp"
    elif raw.startswith(b"BM"):
        extension = "bmp"
    return f"qq-image-{hashlib.sha256(raw).hexdigest()[:16]}.{extension}"


class AssetStore:
    def __init__(
        self, root: Path, *, max_bytes=MAX_STORE_BYTES, max_assets=MAX_ASSETS,
        retention=RETENTION_SECONDS, clock=time.time,
        member_bytes=128 * 1024 * 1024, member_assets=512,
        conversation_bytes=256 * 1024 * 1024, conversation_assets=1024,
    ):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise AssetError("invalid_storage", "文件存储目录不可用。")
        self.blobs = self.root / "blobs"
        self.blobs.mkdir(exist_ok=True, mode=0o700)
        if self.blobs.is_symlink():
            raise AssetError("invalid_storage", "文件存储目录不可用。")
        self.db = self.root / "assets.sqlite3"
        if self.db.is_symlink():
            raise AssetError("invalid_storage", "文件索引不可用。")
        self.max_bytes, self.max_assets = max_bytes, max_assets
        self.retention, self.clock = retention, clock
        self.member_bytes, self.member_assets = member_bytes, member_assets
        self.conversation_bytes, self.conversation_assets = conversation_bytes, conversation_assets
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS assets(
                    ref TEXT PRIMARY KEY, scope TEXT NOT NULL,
                    name TEXT NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL,
                    origin TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS assets_scope ON assets(scope,created);
                CREATE TABLE IF NOT EXISTS receipts(
                    scope TEXT NOT NULL, request_key TEXT NOT NULL, asset_ref TEXT NOT NULL,
                    state TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(scope,request_key,asset_ref)
                );
                CREATE TABLE IF NOT EXISTS grants(
                    scope TEXT NOT NULL, request_key TEXT NOT NULL, asset_ref TEXT NOT NULL,
                    created REAL NOT NULL, PRIMARY KEY(scope,request_key,asset_ref)
                );
                CREATE TABLE IF NOT EXISTS asset_owners(
                    scope TEXT PRIMARY KEY, conversation TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS asset_conversations ON asset_owners(conversation);
            """)

    @classmethod
    def from_env(cls):
        return cls(data_root() / "assets")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _blob(self, digest: str) -> Path:
        if not HASH.fullmatch(digest):
            raise AssetError("invalid_asset", "文件索引无效。")
        path = self.blobs / digest
        if path.is_symlink():
            raise AssetError("invalid_asset", "文件内容不可用。")
        return path

    def _prune(self, db):
        db.execute("DELETE FROM assets WHERE expires<=?", (self.clock(),))
        db.execute("DELETE FROM receipts WHERE created<=?", (self.clock() - self.retention,))
        db.execute("DELETE FROM grants WHERE created<=? OR asset_ref NOT IN (SELECT ref FROM assets)",
                   (self.clock() - 86400,))
        live = {r[0] for r in db.execute("SELECT DISTINCT digest FROM assets")}
        for path in self.blobs.iterdir():
            if HASH.fullmatch(path.name) and path.name not in live and not path.is_symlink():
                path.unlink(missing_ok=True)

    def put(self, scope: Scope, name: str, data: bytes, *, origin="upload", metadata=None) -> dict:
        name = safe_name(name)
        if not isinstance(data, bytes) or len(data) > MAX_ASSET_BYTES:
            raise AssetError("size_limit", "文件超过 40 MiB 处理上限。")
        if origin not in {"upload", "web", "generated", "archive", "converted"}:
            raise AssetError("invalid_origin", "文件来源无效。")
        encoded = json.dumps(metadata or {}, ensure_ascii=False)
        if len(encoded.encode()) > 8192:
            raise AssetError("metadata_limit", "文件说明过长。")
        digest = hashlib.sha256(data).hexdigest()
        created = self.clock()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._prune(db)
            owner = {k: v for k, v in scope.as_dict().items() if k != "actor_id"}
            conversation = hashlib.sha256(json.dumps(owner, sort_keys=True).encode()).hexdigest()
            db.execute("INSERT OR IGNORE INTO asset_owners VALUES(?,?)", (scope.key, conversation))
            existing = db.execute(
                "SELECT * FROM assets WHERE scope=? AND digest=? AND name=? AND metadata=? ORDER BY created DESC LIMIT 1",
                (scope.key, digest, name, encoded),
            ).fetchone()
            if existing:
                return self._public(existing)
            member = db.execute("SELECT COUNT(*),COALESCE(SUM(size),0) FROM assets WHERE scope=?",
                                (scope.key,)).fetchone()
            if member[0] >= self.member_assets or member[1] + len(data) > self.member_bytes:
                raise AssetError("member_quota", "你在当前会话的文件额度已满，请删除不用的文件后继续。")
            group = db.execute("SELECT COUNT(*),COALESCE(SUM(a.size),0) FROM assets a "
                               "LEFT JOIN asset_owners o ON a.scope=o.scope WHERE o.conversation=? OR o.scope IS NULL",
                               (conversation,)).fetchone()
            if group[0] >= self.conversation_assets or group[1] + len(data) > self.conversation_bytes:
                raise AssetError("conversation_quota", "当前会话的文件总额度已满，请清理后继续。")
            rows = db.execute("SELECT COUNT(*),COALESCE(SUM(size),0) FROM assets").fetchone()
            if rows[0] >= self.max_assets or rows[1] + len(data) > self.max_bytes:
                raise AssetError("storage_full", "文件存储空间已满，可删除不再使用的文件后继续。")
            path = self._blob(digest)
            if not path.exists():
                temporary = self.blobs / ("pending_" + secrets.token_hex(16))
                try:
                    with temporary.open("xb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    temporary.chmod(0o600)
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            ref = "asset_" + secrets.token_hex(16)
            db.execute(
                "INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)",
                (ref, scope.key, name, digest, len(data), origin, created, created + self.retention, encoded),
            )
            return self._public(db.execute("SELECT * FROM assets WHERE ref=?", (ref,)).fetchone())

    @staticmethod
    def _public(row) -> dict:
        return {
            "asset_ref": row["ref"], "name": row["name"], "size": row["size"],
            "sha256": row["digest"], "origin": row["origin"],
            "created_at": row["created"], "expires_at": row["expires"],
        }

    def get(self, scope: Scope, ref: str) -> dict:
        row = self._row(scope, ref)
        return self._public(row)

    def metadata(self, scope: Scope, ref: str) -> dict:
        """Read private recipe data only after the usual scoped lookup."""
        return json.loads(self._row(scope, ref)["metadata"])

    def _row(self, scope, ref):
        if not isinstance(ref, str) or not REF.fullmatch(ref):
            raise AssetError("not_found", "文件编号不存在或不属于当前会话。")
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM assets WHERE ref=? AND scope=? AND expires>?",
                (ref, scope.key, self.clock()),
            ).fetchone()
        if row is None:
            raise AssetError("not_found", "文件已过期或不属于当前会话。")
        return row

    def read(self, scope: Scope, ref: str) -> bytes:
        row = self._row(scope, ref)
        path = self._blob(row["digest"])
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_ASSET_BYTES + 1)
        except OSError:
            raise AssetError("missing_content", "文件内容暂不可用，请重新提供文件。") from None
        if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["digest"]:
            raise AssetError("integrity_error", "文件完整性检查失败。")
        return data

    def grant(self, scope: Scope, request_key: str, ref: str):
        self._row(scope, ref)
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 256:
            raise AssetError("invalid_request", "文件请求无法确认。")
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO grants VALUES(?,?,?,?)",
                       (scope.key, request_key, ref, self.clock()))

    def require_visible(self, scope: Scope, ref: str, context: dict):
        row = self._row(scope, ref)
        if row["created"] <= context["created"]:
            return
        with self._connect() as db:
            found = db.execute("SELECT 1 FROM grants WHERE scope=? AND request_key=? AND asset_ref=?",
                               (scope.key, context["request_key"], ref)).fetchone()
        if not found:
            raise AssetError("not_found", "该文件不在当前问题可见范围内。")

    def list(self, scope: Scope, *, cursor=None, limit=30, context=None) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AssetError("invalid_limit", "列表条数应为 1 至 100。")
        upper, last_ref = self.clock() + 1, "z"
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 256:
                    raise ValueError
                upper, last_ref = json.loads(base64.urlsafe_b64decode(cursor))
                if type(upper) not in (int, float) or not math.isfinite(upper) or not REF.fullmatch(last_ref):
                    raise ValueError
            except (ValueError, TypeError):
                raise AssetError("invalid_cursor", "文件列表分页位置无效。") from None
        with self._connect() as db:
            visibility = ""
            extra = ()
            if context is not None:
                visibility = (
                    "AND (created<=? OR ref IN (SELECT asset_ref FROM grants WHERE scope=? AND request_key=?)) "
                )
                extra = (context["created"], scope.key, context["request_key"])
            rows = db.execute(
                "SELECT * FROM assets WHERE scope=? AND expires>? AND (created<? OR (created=? AND ref<?)) "
                + visibility +
                "ORDER BY created DESC,ref DESC LIMIT ?",
                (scope.key, self.clock(), upper, upper, last_ref, *extra, limit + 1),
            ).fetchall()
        return {
            "assets": [self._public(row) for row in rows[:limit]],
            "next_cursor": base64.urlsafe_b64encode(json.dumps([
                rows[limit - 1]["created"], rows[limit - 1]["ref"],
            ]).encode()).decode() if len(rows) > limit else None,
        }

    def delete(self, scope: Scope, ref: str):
        self._row(scope, ref)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM assets WHERE ref=? AND scope=?", (ref, scope.key))
            self._prune(db)

    def reserve_delivery(self, scope: Scope, request_key: str, ref: str) -> bool:
        digest = self._row(scope, ref)["digest"]
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 256:
            raise AssetError("invalid_request", "发送请求无法确认。")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM receipts WHERE scope=? AND request_key=? AND asset_ref=?",
                          (scope.key, request_key, digest)).fetchone():
                return False
            count = db.execute("SELECT COUNT(*) FROM receipts WHERE scope=? AND request_key=?",
                               (scope.key, request_key)).fetchone()[0]
            if count >= 5:
                raise AssetError("delivery_limit", "本轮附件发送已达到 5 个，请先汇总已有结果。")
            cursor = db.execute(
                "INSERT OR IGNORE INTO receipts VALUES(?,?,?,'started',?)",
                (scope.key, request_key, digest, self.clock()),
            )
        return cursor.rowcount == 1

    def mark_delivery(self, scope: Scope, request_key: str, ref: str, state: str):
        if state not in {"sent", "unconfirmed"}:
            raise ValueError("Invalid delivery state")
        digest = self._row(scope, ref)["digest"]
        with self._connect() as db:
            db.execute(
                "UPDATE receipts SET state=? WHERE scope=? AND request_key=? AND asset_ref=? AND state='started'",
                (state, scope.key, request_key, digest),
            )

    def delivery(self, scope: Scope, request_key: str, ref: str) -> str | None:
        digest = self._row(scope, ref)["digest"]
        with self._connect() as db:
            row = db.execute(
                "SELECT state FROM receipts WHERE scope=? AND request_key=? AND asset_ref=?",
                (scope.key, request_key, digest),
            ).fetchone()
        return row[0] if row else None
