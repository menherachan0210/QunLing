"""Short-lived, public tool-result summaries shared within one QQ group.

This is evidence, never an execution capability or a long-term member profile.
Publishers supply trusted installation/group identities and metadata-only text;
file bodies, resource handles and download locations do not belong here.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time

from qunling.assets import AssetError, data_root

TTL_SECONDS = 1800
MAX_PER_GROUP = 12
MAX_TOTAL = 1000
MAX_VERSIONS = 3
MAX_MESSAGE_IDS = 8
MAX_RESULTS = 6
MAX_RESULT_CHARS = 4000
SOURCES = frozenset({"member_info", "avatar_analysis", "group_file_list", "image_delivery", "image_analysis"})

_URL = re.compile(r"(?:[a-z][a-z0-9+.-]*://|data:|www\.)[^\s<>\"']+", re.I)
_REF = re.compile(
    r"\b(?:asset_[0-9a-f]{32}|member_[0-9a-f]{32}|(?:gfile|folder)_[0-9a-f]{24}|(?:avatar|img)_\d+|webfile_[A-Za-z0-9_-]{12,}|(?:file|folder|member|avatar|image|incoming|group_file)_ref_[A-Za-z0-9_-]+)\b"
    r"|\b(?:asset_ref|file_ref|file_id|folder_ref|folder_id|member_ref|avatar_ref|image_ref|incoming_ref|storage_path|task_id)"
    r"[\"']?\s*[:=]\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;<>\"']+)", re.I,
)
_SECRET = re.compile(
    r"(?:\b(?:api[_ -]?key|access[_ -]?token|authorization|password|secret)|密码|口令|令牌)"
    r"[\"']?\s*[:=：是为]?\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;，；<>\"']+)"
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]+|\bsk-[A-Za-z0-9_-]{12,}", re.I,
)
_GENERIC = {"这个", "那个", "什么", "怎么", "一下", "看看", "可以", "现在", "刚才", "继续", "好的", "我们", "你们", "他们", "他的", "她的", "这个", "的头", "的群", "一下", "里的", "the", "and", "that", "this", "with", "what"}
_FOLLOWUP = re.compile(r"^(?:那(?:么)?[，, ]*)?(?:他|她|它|这(?:个|张|份|些|件|种)?|刚才|上面|继续|详细|说说|再说|为什么|为啥|what\b|why\b|then\b|it\b)", re.I)


def _safe_text(value, limit):
    if not isinstance(value, str):
        return ""
    value = value[:limit * 3]
    value = _SECRET.sub("[凭证已省略]", value)
    value = _URL.sub("[地址已省略]", value)
    value = _REF.sub("[内部引用已省略]", value)
    return "".join(c for c in value if c in "\n\t" or ord(c) >= 32 and ord(c) != 127).strip()[:limit]


def workspace_id(instance_uuid, workspace_uuid):
    """Match integration.workspace_key without creating a legacy fallback key."""
    if not all(isinstance(v, str) and 0 < len(v) <= 256 for v in (instance_uuid, workspace_uuid)):
        return ""
    return hashlib.sha256(json.dumps([instance_uuid, workspace_uuid]).encode()).hexdigest()


def _scope(workspace, bot, group):
    if not isinstance(workspace, str) or not re.fullmatch(r"[0-9a-f]{64}", workspace):
        return None
    if not isinstance(bot, str) or not 1 <= len(bot) <= 128 or any(ord(c) < 33 for c in bot):
        return None
    if type(group) not in (str, int):
        return None
    group = str(group)
    if not group.isascii() or not group.isdigit() or not 1 <= len(group) <= 20 or int(group) <= 0:
        return None
    return workspace, bot, str(int(group))


def _identity(value):
    if type(value) not in (str, int):
        return ""
    value = str(value)
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else ""


def _message_ids(values):
    if not isinstance(values, (list, tuple)) or len(values) > 120:
        return ()
    return tuple(dict.fromkeys(identity for value in values if (identity := _identity(value))))


def _connect(*, create):
    root = data_root()
    path = root / "group-knowledge.sqlite3"
    if not create and not path.exists():
        return None
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        return None
    db = sqlite3.connect(path, timeout=.15)
    db.row_factory = sqlite3.Row
    if create:
        db.execute("""CREATE TABLE IF NOT EXISTS evidence (
            id INTEGER PRIMARY KEY, workspace TEXT NOT NULL, bot TEXT NOT NULL,
            group_id TEXT NOT NULL, question TEXT NOT NULL, source TEXT NOT NULL,
            subject TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL,
            message_ids TEXT NOT NULL DEFAULT '[]', actor_id TEXT NOT NULL DEFAULT '')""")
        # Existing installations keep their valid summaries. Serialize schema
        # migration with other publishers, including separate plugin processes.
        with db:
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(evidence)")}
            for column, declaration in (("message_ids", "TEXT NOT NULL DEFAULT '[]'"),
                                        ("actor_id", "TEXT NOT NULL DEFAULT ''")):
                if column not in columns:
                    db.execute(f"ALTER TABLE evidence ADD COLUMN {column} {declaration}")
        db.execute("CREATE INDEX IF NOT EXISTS evidence_scope ON evidence(workspace,bot,group_id,created)")
    return db


def publish(*, workspace_id, bot_id, group_id, question, source, subject, text,
            message_ids=(), actor_id=""):
    """Best-effort publish by a trusted tool after a successful read.

    Only public metadata/analysis and confirmed image-delivery facts are accepted. The
    caller must never publish a file's body or private per-request task state.
    message_ids and actor_id must come from trusted Host/query context, never
    model parameters. Delivery does not imply that output pixels were inspected.
    No network call, model call, or file grant occurs here.
    """
    scope = _scope(workspace_id, bot_id, group_id)
    if scope is None or not isinstance(source, str) or source not in SOURCES:
        return False
    question, subject, text = _safe_text(question, 300), _safe_text(subject, 160), _safe_text(text, 1200)
    if not subject or not text:
        return False
    db = None
    try:
        db = _connect(create=True)
        if db is None:
            return False
        now = time.time()
        with db:
            db.execute("DELETE FROM evidence WHERE created <= ? OR created > ?", (now - TTL_SECONDS, now))
            db.execute("INSERT INTO evidence(workspace,bot,group_id,question,source,subject,text,created,message_ids,actor_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (*scope, question, source, subject, text, now,
                        json.dumps(_message_ids(message_ids)[:MAX_MESSAGE_IDS]), _identity(actor_id)))
            # A queued follow-up still needs the version visible at its receipt
            # time even if another query refreshes the same subject meanwhile.
            db.execute("DELETE FROM evidence WHERE workspace=? AND bot=? AND group_id=? AND source=? AND subject=? AND id NOT IN (SELECT id FROM evidence WHERE workspace=? AND bot=? AND group_id=? AND source=? AND subject=? ORDER BY created DESC,id DESC LIMIT ?)",
                       (*scope, source, subject, *scope, source, subject, MAX_VERSIONS))
            db.execute("DELETE FROM evidence WHERE workspace=? AND bot=? AND group_id=? AND id NOT IN (SELECT id FROM evidence WHERE workspace=? AND bot=? AND group_id=? ORDER BY created DESC,id DESC LIMIT ?)", (*scope, *scope, MAX_PER_GROUP))
            db.execute("DELETE FROM evidence WHERE id NOT IN (SELECT id FROM evidence ORDER BY created DESC,id DESC LIMIT ?)", (MAX_TOTAL,))
        return True
    except (OSError, sqlite3.Error, AssetError):
        return False
    finally:
        if db is not None:
            db.close()


def clear_group(*, workspace_id, bot_id, group_id):
    """Delete one proven group scope; errors must reach the reset coordinator."""
    scope = _scope(workspace_id, bot_id, group_id)
    if scope is None:
        raise ValueError("Invalid group knowledge scope")
    if (data_root() / "group-knowledge.sqlite3").is_symlink():
        raise ValueError("Invalid group knowledge storage")
    db = _connect(create=False)
    if db is None:
        return
    try:
        with db:
            db.execute("DELETE FROM evidence WHERE workspace=? AND bot=? AND group_id=?", scope)
    finally:
        db.close()


def _terms(value):
    value = value.casefold()
    terms = set(re.findall(r"[a-z0-9_]{3,}", value))
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        terms.update(run[i:i + 2] for i in range(len(run) - 1))
    return terms - _GENERIC


def _score(row, query, terms):
    subject = row["subject"].casefold()
    if len(subject) >= 2 and subject in query.casefold():
        return 20
    overlap = _terms(row["question"] + " " + row["subject"] + " " + row["text"]) & terms
    # One generic two-character fragment must not attach a different member's
    # result to the current conversation. Exact names/IDs and richer overlap do.
    return len(overlap) if len(overlap) >= 2 or any(re.fullmatch(r"[a-z0-9_]{3,}", t) for t in overlap) else 0


def recall(*, workspace_id, bot_id, group_id, question, nearby_text="", before=None,
           related_message_ids=()):
    """Read relevant public summaries, never use this during participation judging.

    `before` freezes evidence to the trusted Host receipt time, which preserves
    same-second ordering unlike rounded platform timestamps. A short follow-up
    may use its own already-authorized topic text to identify the earlier subject.
    Missing storage and contention fail closed without affecting chat delivery.
    """
    scope = _scope(workspace_id, bot_id, group_id)
    question, nearby = _safe_text(question, 800), _safe_text(nearby_text, 1600)
    if scope is None or not question:
        return []
    now = time.time()
    if before is not None and (type(before) not in (int, float) or not math.isfinite(before) or before <= 0):
        return []
    boundary = min(now, before) if before is not None else now
    db = None
    try:
        db = _connect(create=False)
        if db is None:
            return []
        columns = {row[1] for row in db.execute("PRAGMA table_info(evidence)")}
        lineage = ("message_ids,actor_id" if {"message_ids", "actor_id"} <= columns
                   else "'[]' AS message_ids,'' AS actor_id")
        rows = db.execute(f"SELECT question,source,subject,text,created,{lineage} FROM evidence WHERE workspace=? AND bot=? AND group_id=? AND created>? AND created<=? ORDER BY created DESC,id DESC LIMIT ?", (*scope, now - TTL_SECONDS, boundary, MAX_PER_GROUP)).fetchall()
    except (OSError, sqlite3.Error, AssetError):
        return []
    finally:
        if db is not None:
            db.close()
    direct = _terms(question)
    # Nearby text supplements only a short genuine continuation, not an
    # independent new request that happens to follow another topic.
    use_nearby = len(question) <= 100 and bool(_FOLLOWUP.match(question.strip()))
    neighbor_terms = _terms(nearby) if use_nearby else set()
    related = set(_message_ids(related_message_ids))
    ranked = []
    for row in rows:
        if row["source"] not in SOURCES:
            continue
        try:
            linked = bool(related.intersection(_message_ids(json.loads(row["message_ids"]))))
        except (TypeError, ValueError):
            linked = False
        # Actual message/topic lineage can identify terse corrections without
        # guessing from whichever member was most recently looked up.
        score = 40 if linked else _score(row, question, direct)
        if not score and neighbor_terms:
            score = _score(row, nearby, neighbor_terms) * .5
        if score:
            ranked.append((score, row))
    ranked.sort(key=lambda item: (item[0], item[1]["created"]), reverse=True)
    output, remaining, selected = [], MAX_RESULT_CHARS, set()
    for _, row in ranked:
        key = row["source"], row["subject"]
        if key in selected:
            continue
        # Re-sanitize at read time too: an older version or damaged row must
        # never become a source of URLs/handles/credentials in another turn.
        subject = _safe_text(row["subject"], 160)
        text = _safe_text(row["text"], min(1200, max(0, remaining - len(subject))))
        if not text:
            break
        item = {"source": row["source"], "subject": subject, "text": text, "timestamp": int(row["created"])}
        actor = _identity(row["actor_id"])
        if actor:
            item["actor_id"] = actor
        output.append(item)
        selected.add(key)
        remaining -= len(subject) + len(text)
        if remaining <= 0 or len(output) >= MAX_RESULTS:
            break
    return output
