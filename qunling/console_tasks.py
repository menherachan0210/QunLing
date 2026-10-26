"""Read-only operator view of background tasks; never returns payloads or paths."""
from __future__ import annotations

from contextlib import closing
import json
import re
import sqlite3
import time

from qunling.assets import data_root
from qunling.group_knowledge import workspace_id
from qunling.request_journal import _cursor
from qunling.tasks.models import TaskStatus


def parse_filters(values):
    if not isinstance(values, dict) or set(values) - {'group', 'status', 'limit', 'before', 'task_id'}:
        raise ValueError('Invalid task filters')
    result = {key: str(value) for key, value in values.items() if value != ''}
    if any(len(value) > 200 for value in result.values()):
        raise ValueError('Invalid task filter length')
    if 'group' in result and not re.fullmatch(r'[1-9][0-9]{0,19}', result['group']):
        raise ValueError('Invalid group')
    if 'status' in result and result['status'] not in {s.value for s in TaskStatus}:
        raise ValueError('Invalid task status')
    if 'task_id' in result and not re.fullmatch(r'[a-f0-9]{32}', result['task_id']):
        raise ValueError('Invalid task ID')
    if 'before' in result:
        _cursor(result['before'])
    result['limit'] = int(result.get('limit', 30))
    if not 1 <= result['limit'] <= 100:
        raise ValueError('Invalid task limit')
    return result


def listing(path, workspace, bot, filters, *, now=None):
    now = time.time() if now is None else now
    empty = dict(available=True, tasks=[], counts={}, next_before=None, sampled_at=now)
    if path.is_symlink():
        raise OSError('Invalid task store')
    if not path.exists():
        return empty
    # Read-only avoids starting workers, creating tables, recovering leases or replaying sends.
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ql_tasks'").fetchone():
            return empty
        base = "json_extract(scope_json,'$.workspace_id')=? AND json_extract(scope_json,'$.bot_id')=?"
        args = [workspace, bot]
        if filters.get('group'):
            base += " AND json_extract(scope_json,'$.conversation_id')=?"
            args.append('group:' + filters['group'])
        counts = {row[0]: row[1] for row in db.execute(f'SELECT status,count(*) FROM ql_tasks WHERE {base} GROUP BY status', args)}
        where, params = base, list(args)
        for field, column in [('status', 'status'), ('task_id', 'id')]:
            if filters.get(field):
                where += f' AND {column}=?'
                params.append(filters[field])
        if filters.get('before'):
            timestamp, identifier = _cursor(filters['before'])
            where += ' AND (created_at<? OR (created_at=? AND id<?))'
            params += [timestamp, timestamp, identifier]
        rows = db.execute(f'SELECT * FROM ql_tasks WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ?',
                          [*params, filters['limit'] + 1]).fetchall()
        more, rows = len(rows) > filters['limit'], rows[:filters['limit']]
        has_outbox = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ql_file_outbox'").fetchone()
        tasks = []
        for row in rows:
            scope = json.loads(row['scope_json'])
            checkpoint = json.loads(row['checkpoint'])
            delivery = db.execute('SELECT state,error_code,updated FROM ql_file_outbox WHERE task_id=? AND scope_key=? ORDER BY created DESC,id DESC LIMIT 1',
                                  (row['id'], row['scope_key'])).fetchone() if has_outbox else None
            item = {key: row[key] for key in ('id', 'status', 'created_at', 'updated_at', 'attempts', 'max_attempts', 'deadline_at', 'retry_at', 'error_code')}
            item.update(conversation=scope['conversation_id'], actor=scope['actor_id'],
                        stage=checkpoint.get('stage') if checkpoint.get('stage') in {'processing', 'processed'} else '',
                        result_count=len(json.loads(row['result_refs'])),
                        delivery=delivery['state'] if delivery else 'not_scheduled',
                        delivery_error=delivery['error_code'] if delivery else None,
                        elapsed_seconds=round(max(0, (row['updated_at'] if row['status'] in {'completed', 'failed', 'cancelled'} else now) - row['created_at']), 1))
            tasks.append(item)
        return dict(available=True, tasks=tasks, counts=counts, sampled_at=now,
                    next_before=f"{rows[-1]['created_at']}:{rows[-1]['id']}" if more else None)


async def tasks(ap, context, bot, filters):
    import asyncio
    from langbot.pkg.api.http.authz import Permission, require_permission
    require_permission(context, Permission.RESOURCE_MANAGE)
    info = await ap.bot_service.get_bot(context, bot, include_secret=False)
    if not info or info.get('adapter') != 'onebot-v11':
        raise PermissionError('Bot not in this workspace')
    binding = await ap.workspace_service.get_execution_binding(context.workspace_uuid)
    return await asyncio.to_thread(listing, data_root() / 'tasks.sqlite',
                                   workspace_id(binding.instance_uuid, context.workspace_uuid), bot, filters)
