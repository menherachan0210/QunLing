"""Operator snapshots of plugin-owned jobs, isolated from the file scheduler."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time

from qunling.assets import data_root
from qunling.group_knowledge import workspace_id
from qunling.scope import Scope


def publish(path, scope, job):
    if not isinstance(scope, Scope) or not re.fullmatch('[a-f0-9]{32}', job['id']):
        raise ValueError('Invalid task owner or identifier')
    stage = job['status']
    status = {'downloading': 'running', 'queued': 'pending', 'awaiting_password': 'waiting',
              'interrupted': 'failed'}.get(stage, stage)
    if status not in {'pending', 'running', 'waiting', 'partial', 'completed', 'failed', 'cancelled'}:
        raise ValueError('Invalid task status')
    progress = job.get('download', {})
    checkpoint = {
        'stage': 'translating' if stage == 'running' else stage,
        'downloaded_bytes': int(progress.get('received', 0)),
        'total_bytes': int(progress.get('total', 0)),
        'completed_pages': sum(p.get('result') is not None for p in job['pages']),
        'total_pages': len(job['pages']),
        'delivery': {'unknown': 'unconfirmed'}.get(job.get('delivery'), job.get('delivery', 'pending')),
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise OSError('Invalid task database')
    with sqlite3.connect(path, timeout=5) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS ql_plugin_jobs (
            id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, scope_json TEXT NOT NULL,
            kind TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
            updated_at REAL NOT NULL, checkpoint TEXT NOT NULL, error_code TEXT)''')
        db.execute('DELETE FROM ql_plugin_jobs WHERE updated_at<?', (time.time() - 7 * 86400,))
        db.execute('''INSERT INTO ql_plugin_jobs VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at,
            checkpoint=excluded.checkpoint,error_code=excluded.error_code
            WHERE ql_plugin_jobs.scope_key=excluded.scope_key''', (
                job['id'], scope.key, json.dumps(scope.as_dict()), 'comic_translation', status,
                job['created'], job['updated'], json.dumps(checkpoint), job.get('error_code') or None))


def publish_comic(plugin, job):
    getter = getattr(plugin, 'get_installation_binding', None)
    binding = getter() if callable(getter) else None
    if binding is None or not os.environ.get('QUNLING_DATA_DIR'):
        return
    workspace = workspace_id(str(binding.instance_uuid), str(binding.workspace_uuid))
    target = job['target']
    scope = Scope(workspace, target['bot_uuid'], target['chat_type'] + ':' + target['chat_id'], target['sender_id'])
    publish(data_root() / 'tasks.sqlite', scope, job)
