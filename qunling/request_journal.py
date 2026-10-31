"""Bounded operational metadata for QQ requests, never prompts or tool payloads.

This is an audit journal, not an execution queue. A new Host process closes
abandoned entries without replaying any model call, tool or message delivery.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

BOOT_ID = uuid.uuid4().hex
MAX_RECORDS = 2000
MAX_EVENTS = 96
KEEP_SECONDS = 7 * 86400
PHASES = frozenset({'received', 'queued', 'preparing', 'runtime_wait', 'running', 'model',
                    'tool_wait', 'tool', 'retry_wait', 'resuming', 'sending', 'delivered', 'ended'})
STATUSES = frozenset({'active', 'completed', 'processed', 'failed', 'cancelled', 'interrupted', 'unconfirmed', 'rejected'})
REASONS = frozenset({'', 'queue_full', 'queue_timeout', 'execution_timeout', 'tool_timeout',
    'tool_queue_timeout', 'runtime_unavailable', 'upstream_timeout', 'rate_limited', 'authentication',
    'permission', 'network', 'tool_failed', 'pipeline_failed', 'no_confirmed_reply',
    'delivery_unconfirmed', 'upstream_unavailable', 'restart_before_execution', 'restart_interrupted',
    'restart_unconfirmed', 'cancelled', 'message_recalled', 'shutdown', 'capacity', 'admission_failed'})


def classify(error, *, phase='running', _depth=0):
    """Inspect type/status only. Exception text can contain credentials or chats."""
    name = type(error).__name__
    if name == 'ResourceBusy':
        return 'capacity'
    response = getattr(error, 'response', None)
    status = getattr(error, 'status_code', None) or getattr(response, 'status_code', None)
    if status in (401, 403):
        return 'authentication' if status == 401 else 'permission'
    if status == 429:
        return 'rate_limited'
    if isinstance(status, int) and 500 <= status < 600:
        return 'upstream_unavailable'
    if name == 'RunnerExecutionError' and getattr(error, 'error_code', None) == 'runner.timeout':
        return 'upstream_timeout'
    if name in {'ConnectionClosedError', 'PluginRuntimeNotConnectedError', 'RunnerNotFoundError'}:
        return 'runtime_unavailable'
    if isinstance(error, TimeoutError) or name in {'ActionCallTimeoutError', 'ReadTimeout', 'ConnectTimeout', 'APITimeoutError'}:
        return {'tool': 'tool_timeout', 'tool_wait': 'tool_queue_timeout', 'runtime_wait': 'runtime_unavailable'}.get(phase, 'upstream_timeout')
    if name in {'PermissionError', 'PermissionDeniedError', 'RunnerNotAuthorizedError', 'WorkspaceAccessDeniedError', 'WorkspaceBindingStaleError', 'WorkspaceNotFoundError'}:
        return 'permission'
    if name in {'ConnectError', 'ConnectionError', 'APIConnectionError'}:
        return 'network'
    if _depth < 3 and getattr(error, '__cause__', None) is not None:
        return classify(error.__cause__, phase=phase, _depth=_depth+1)
    return 'tool_failed' if phase == 'tool' else 'pipeline_failed'


def _id(value):
    if value is None or isinstance(value, bool):
        raise ValueError('Missing diagnostic identity')
    value = str(value)
    if not re.fullmatch(r'[a-zA-Z0-9_.:@-]{1,160}', value):
        raise ValueError('Invalid diagnostic identity')
    return value


def _tool(value):
    return value if isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', value) else ''


def parse_filters(values):
    """Strict, shared query validation for the operator API and its proxy."""
    if set(values) - {'group', 'status', 'limit', 'before', 'request_id'}:
        raise ValueError('Unknown diagnostic filter')
    result = {key: str(value) for key, value in values.items() if value != ''}
    if any(len(value) > 200 for value in result.values()):
        raise ValueError('Filter too long')
    if 'group' in result and not re.fullmatch(r'[0-9]{1,20}', result['group']):
        raise ValueError('Invalid group')
    if 'status' in result and result['status'] not in STATUSES:
        raise ValueError('Invalid status')
    if 'request_id' in result:
        _id(result['request_id'])
    if 'limit' in result:
        result['limit'] = int(result['limit'])
        if not 1 <= result['limit'] <= 100:
            raise ValueError('Invalid limit')
    if 'before' in result:
        _cursor(result['before'])
    return result


def _cursor(value):
    timestamp, separator, request_id = str(value).partition(':')
    timestamp = float(timestamp)
    if not separator or not math.isfinite(timestamp) or not 0 < timestamp < 1e12:
        raise ValueError('Invalid cursor')
    return timestamp, _id(request_id)


class Journal:
    def __init__(self, path, instance, *, boot_id=BOOT_ID, clock=time.time):
        self.path, self.instance, self.boot_id, self.clock = Path(path), _id(instance), boot_id, clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY, instance TEXT NOT NULL, workspace TEXT NOT NULL,
                bot TEXT NOT NULL, group_id TEXT NOT NULL, sender TEXT NOT NULL, message_id TEXT NOT NULL,
                origin TEXT NOT NULL, boot_id TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                phase TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, tool TEXT NOT NULL,
                attempts INTEGER NOT NULL, uncertain INTEGER NOT NULL, events TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS requests_scope ON requests(instance,workspace,bot,created)')
            # Each process opens one journal per instance. Reopening in the same
            # process does not interrupt its own requests.
            now = self.clock()
            stale = db.execute("SELECT request_id,phase,uncertain,events FROM requests WHERE instance=? AND boot_id<>? AND status='active'",
                               (self.instance, self.boot_id)).fetchall()
            for row in stale:
                reason = ('restart_unconfirmed' if row['uncertain'] else
                          'restart_before_execution' if row['phase'] in {'received','queued','preparing','runtime_wait'} else 'restart_interrupted')
                events = self._events(row['events'], now, 'ended', reason)
                db.execute('UPDATE requests SET status=?,phase=?,reason=?,updated=?,events=? WHERE request_id=?',
                           ('unconfirmed' if row['uncertain'] else 'interrupted', 'ended', reason, now, events, row['request_id']))
            self.prune(db, now)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=.05)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def prune(self, db, now):
        db.execute("DELETE FROM requests WHERE status<>'active' AND updated<?", (now-KEEP_SECONDS,))
        db.execute("DELETE FROM requests WHERE request_id IN (SELECT request_id FROM requests "
                   "WHERE origin='participation' AND status<>'active' ORDER BY created DESC LIMIT -1 OFFSET 400)")
        db.execute("""DELETE FROM requests WHERE request_id IN (
            SELECT request_id FROM requests WHERE status<>'active' ORDER BY created DESC LIMIT -1 OFFSET ?)""", (MAX_RECORDS,))

    @staticmethod
    def _events(previous, now, phase, reason='', tool='', attempt=0, details=None):
        events = json.loads(previous)
        entry = dict(at=now, phase=phase, reason=reason, tool=tool, attempt=attempt)
        if details:
            from qunling.activity import details as clean_details
            entry['details'] = clean_details(details)
        events.append(entry)
        if len(events) > MAX_EVENTS:
            events = [events[0], *events[-(MAX_EVENTS-1):]]
        return json.dumps(events, separators=(',', ':'))

    def start(self, query, owner, *, origin='mention', at=None):
        instance, workspace, _, bot, group, sender = owner
        assert _id(instance) == self.instance
        request_id = _id(query.query_uuid) if query.query_uuid else 'admission-' + uuid.uuid4().hex
        message_id = getattr(query.message_event, 'message_id', None)
        message_id = '' if message_id is None else _id(message_id)
        now = self.clock() if at is None else at
        events = self._events('[]', now, 'received')
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (request_id, self.instance, _id(workspace), _id(bot), _id(group), _id(sender), message_id,
                 origin if origin in {'continuation', 'participation'} else 'mention', self.boot_id, now, now,
                 'received', 'active', '', '', 0, 0, events))
            self.prune(db, now)
        return request_id

    def record(self, request_id, phase, *, status='active', reason='', tool='', attempt=0, uncertain=None, at=None, details=None):
        if phase not in PHASES or status not in STATUSES or reason not in REASONS:
            raise ValueError('Invalid diagnostic code')
        tool, now = _tool(tool), self.clock() if at is None else at
        with self.connect() as db:
            row = db.execute('SELECT * FROM requests WHERE request_id=? AND instance=? AND boot_id=?',
                             (request_id, self.instance, self.boot_id)).fetchone()
            if row is None or row['status'] != 'active':
                return
            events = self._events(row['events'], now, phase, reason, tool, attempt, details)
            db.execute('''UPDATE requests SET phase=?,status=?,reason=?,tool=?,updated=?,attempts=?,
                uncertain=?,events=? WHERE request_id=?''',
                (phase, status, reason, tool, now, max(row['attempts'], attempt),
                 row['uncertain'] if uncertain is None else int(bool(uncertain)), events, request_id))

    def listing(self, workspace, bot, *, group='', status='', limit=50, before=None, request_id=''):
        limit = max(1, min(100, int(limit)))
        where, args = 'instance=? AND workspace=? AND bot=?', [self.instance, _id(workspace), _id(bot)]
        if group:
            where += ' AND group_id=?'; args.append(_id(group))
        if status:
            if status not in STATUSES:
                raise ValueError('Invalid status filter')
            where += ' AND status=?'; args.append(status)
        if request_id:
            where += ' AND request_id=?'; args.append(_id(request_id))
        if before is not None:
            timestamp, cursor_id = _cursor(before)
            where += ' AND (created<? OR (created=? AND request_id<?))'
            args.extend((timestamp, timestamp, cursor_id))
        with self.connect() as db:
            self.prune(db, self.clock())
            rows = db.execute(f'SELECT * FROM requests WHERE {where} ORDER BY created DESC,request_id DESC LIMIT ?', [*args, limit+1]).fetchall()
        more, rows = len(rows) > limit, rows[:limit]
        result = []
        for row in rows:
            item = {key: row[key] for key in ('request_id','group_id','sender','message_id','origin','created','updated','phase','status','reason','tool','attempts')}
            item['elapsed_seconds'] = round(max(0, (self.clock() if row['status']=='active' else row['updated'])-row['created']), 1)
            item['operation_unconfirmed'] = bool(row['uncertain'])
            from qunling.performance import request_timings
            item['timing'] = request_timings(json.loads(row['events']),
                self.clock() if row['status'] == 'active' else row['updated'], max_events=MAX_EVENTS)
            if request_id:
                item['events'] = json.loads(row['events'])
            result.append(item)
        return dict(available=True, sampled_at=self.clock(), retention_days=7, limit=limit, requests=result,
                    next_before=f"{rows[-1]['created']}:{rows[-1]['request_id']}" if more else None)


def get_journal(ap, instance):
    root = os.environ.get('QUNLING_DATA_DIR')
    if not root or not Path(root).is_absolute():
        return None
    stores = getattr(ap, '_request_journals', None)
    if stores is None:
        stores = {}; ap._request_journals = stores
    if instance not in stores:
        key = hashlib.sha256(_id(instance).encode()).hexdigest()[:32]
        from qunling.journal_writer import QueuedJournal
        stores[instance] = QueuedJournal(Path(root)/'requests'/f'{key}.sqlite3', instance)
    return stores[instance]


def attach(ap, query, owner, *, origin='mention'):
    try:
        store = get_journal(ap, owner[0])
        if store is not None:
            request_id = store.start(query, owner, origin=origin)
            object.__setattr__(query, '_request_journal', (store, request_id))
    except (OSError, sqlite3.Error, ValueError, AttributeError):
        ap._request_diagnostics_degraded = True
        ap.logger.warning('Request diagnostic storage unavailable; request execution continues')


def record(query, phase, **values):
    attached = getattr(query, '_request_journal', None)
    if attached is None:
        return
    try:
        attached[0].record(attached[1], phase, **values)
    except (OSError, sqlite3.Error, ValueError):
        # Failure is visible to the next management read. Never break a reply
        # or retry an external operation because its diagnostics write failed.
        attached[0].write_failed = True


def failure(query, error):
    reason = classify(error, phase=getattr(query, '_request_phase', 'running'))
    object.__setattr__(query, '_request_error', reason)
    record(query, getattr(query, '_request_phase', 'running'), reason=reason)


def stage_started(query, stage):
    from qunling.qq_dispatch import stage_progress

    stage_progress(query)
    kind = type(stage).__name__
    phase = 'sending' if kind == 'SendResponseBackStage' else 'model' if kind == 'Processor' else 'running'
    object.__setattr__(query, '_request_phase', phase)
    record(query, phase, uncertain=phase=='sending')


def stage_finished(query):
    if getattr(query, '_qq_delivery_confirmed', False):
        record(query, 'delivered', uncertain=False)


async def diagnostics(ap, context, bot_uuid, filters):
    # Deployment operator diagnostics, not an agent-readable transcript API.
    from langbot.pkg.api.http.authz import Permission, require_permission
    require_permission(context, Permission.RESOURCE_MANAGE)
    info = await ap.bot_service.get_bot(context, bot_uuid, include_secret=False)
    if not info or info.get('adapter') != 'onebot-v11':
        raise PermissionError('Bot not in current workspace')
    binding = await ap.workspace_service.get_execution_binding(context.workspace_uuid)
    store = get_journal(ap, binding.instance_uuid)
    if store is None:
        return dict(available=False, requests=[])
    import asyncio
    result = await asyncio.to_thread(store.listing, context.workspace_uuid, bot_uuid, **filters)
    if getattr(store, 'write_failed', False) or getattr(ap, '_request_diagnostics_degraded', False):
        result['write_degraded'] = True
    return result
