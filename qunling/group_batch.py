"""Bounded Host-observed image identities shared with the group reader.

Only metadata is retained. URLs, pixels and tool permissions never enter this
cache. The reader must resolve and verify every message against the platform.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time

from qunling.assets import data_root

TTL = 900
MAX_RECORDS = 128
MAX_IMAGES = 8


def _key(workspace, bot, group, sender, anchor):
    values = tuple(str(value) for value in (workspace, bot, group, sender, anchor))
    if (not re.fullmatch(r"[0-9a-f]{64}", values[0])
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", values[1])
            or any(not re.fullmatch(r"[1-9][0-9]{0,19}", value) for value in values[2:4])
            or not re.fullmatch(r"-?[1-9][0-9]{0,19}", values[4])):
        raise ValueError("Invalid batch scope")
    return values


def _connect(create=False):
    path = data_root() / "group-batches.sqlite3"
    if path.is_symlink() or (not create and not path.is_file()):
        return None
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=.15)
    if create:
        db.execute("""CREATE TABLE IF NOT EXISTS batches (
            workspace TEXT, bot TEXT, group_id TEXT, sender TEXT, anchor TEXT,
            created REAL NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(workspace,bot,group_id,sender,anchor))""")
    return db


def publish(workspace, bot, group, sender, anchor, messages, bot_account):
    """Called only for a validated, frozen Host participation batch."""
    db = None
    try:
        key = _key(workspace, bot, group, sender, anchor)
        rows = []
        for message in messages:
            if (message.bot_id != bot or message.group_id != str(group) or not message.has_media
                    or not re.fullmatch(r"-?[1-9][0-9]{0,19}", message.message_id)):
                continue
            author = str(bot_account if message.is_bot else message.sender_id)
            if not re.fullmatch(r"[1-9][0-9]{0,19}", author):
                continue
            rows.append(dict(message_id=message.message_id, sender_id=author,
                sender_name=message.sender_name[:80], time=int(message.timestamp),
                text=message.text[:1000], reply_message_id=message.reply_to))
        if not rows:
            return False
        db = _connect(create=True)
        if db is None:
            return False
        now = time.time()
        with db:
            db.execute("DELETE FROM batches WHERE created < ?", (now - TTL,))
            db.execute("INSERT OR REPLACE INTO batches VALUES (?,?,?,?,?,?,?)",
                       (*key, now, json.dumps(rows[-MAX_IMAGES:], ensure_ascii=False)))
            db.execute("DELETE FROM batches WHERE rowid NOT IN (SELECT rowid FROM batches ORDER BY created DESC, rowid DESC LIMIT ?)",
                       (MAX_RECORDS,))
        return True
    except Exception:
        return False
    finally:
        if db is not None:
            db.close()


def recall(workspace, bot, group, sender, anchor):
    """Return only this request owner's frozen image identities, never live data."""
    db = None
    try:
        key = _key(workspace, bot, group, sender, anchor)
        db = _connect()
        if db is None:
            return []
        row = db.execute("SELECT created,payload FROM batches WHERE workspace=? AND bot=? AND group_id=? AND sender=? AND anchor=?", key).fetchone()
        if row is None or not 0 <= time.time() - row[0] < TTL or len(row[1]) > 24000:
            return []
        rows = json.loads(row[1])
        return rows if isinstance(rows, list) and len(rows) <= MAX_IMAGES else []
    except Exception:
        return []
    finally:
        if db is not None:
            db.close()


def _platform_identity(value, *, signed=False):
    text = str(value)
    if not re.fullmatch(r"-?[1-9][0-9]{0,19}" if signed else r"[1-9][0-9]{0,19}", text):
        raise ValueError("Invalid platform identity")
    return text


async def merge_history(plugin, context, history, validate_url):
    from qunling.integration import workspace_key
    rows = await asyncio.to_thread(recall, workspace_key(plugin), context.bot,
                                   context.group, context.sender, context.anchor)
    existing = {str(row['message_id']): row for row in history['messages']}
    pending = [row for row in rows if not existing.get(str(row.get('message_id')), {}).get('images')]
    if not pending:
        return history
    slots = asyncio.Semaphore(2)

    async def resolve(expected):
        async with slots:
            try:
                message_id = _platform_identity(expected['message_id'], signed=True)
                async with asyncio.timeout(4):
                    raw = await plugin.call_platform_api(context.bot, 'call_platform_api',
                        {'action': 'get_msg', 'params': {'message_id': int(message_id)}})
                if (not isinstance(raw, dict)
                        or _platform_identity(raw.get('message_id'), signed=True) != message_id
                        or _platform_identity(raw.get('group_id')) != context.group
                        or _platform_identity((raw.get('sender') or {}).get('user_id', raw.get('user_id'))) != expected['sender_id']):
                    return None
                segments = raw.get('message')
                if not isinstance(segments, list) or len(segments) > 2000:
                    return None
                images = []
                for index, segment in enumerate(segments):
                    if not isinstance(segment, dict) or segment.get('type') != 'image':
                        continue
                    data = segment.get('data')
                    if not isinstance(data, dict):
                        continue
                    try:
                        url = validate_url(data.get('url') or data.get('file'))
                    except Exception:
                        continue
                    images.append({'index': index, 'url': url})
                    if len(images) == 4:
                        break
                return {**expected, 'images': images, 'batch_image': True} if images else None
            except Exception:
                return None

    # Eight identities maximum; timeouts are isolated to avoid blocking text.
    resolved = await asyncio.gather(*(resolve(row) for row in pending))
    for row in resolved:
        if row is not None:
            existing[row['message_id']] = row
    merged = sorted(existing.values(), key=lambda row: int(row['time']))
    limit = history.get('max_messages', 1000)
    if len(merged) > limit:
        history['truncated'] = True
    history['messages'] = merged[-limit:]
    history['batch_before_time'] = max([context.timestamp, *[int(row['time']) for row in rows]])
    history['batch_images_unavailable'] = sum(row is None for row in resolved)
    return history
