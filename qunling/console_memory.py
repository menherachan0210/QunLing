"""Authenticated group boundary for the plugin's memory management console."""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone

from qunling import group_reset
from qunling.group_reset_host import MemoryResetError


def validate(values):
    if (not isinstance(values, dict) or not isinstance(values.get('group_id'), str)
            or not re.fullmatch(r'[1-9][0-9]{0,19}', values['group_id'])):
        raise ValueError('Invalid group')
    action = values.get('action')
    common = {'group_id', 'action'}
    if action == 'list':
        allowed = common | {'query', 'kind', 'offset'}
        if (not isinstance(values.get('query', ''), str) or len(values.get('query', '')) > 100
                or values.get('kind', 'all') not in {'all', 'profile', 'episode', 'interaction'}
                or type(values.get('offset', 0)) is not int or not 0 <= values.get('offset', 0) <= 5000):
            raise ValueError('Invalid memory filters')
    elif action == 'export':
        allowed = common
    elif action in {'save', 'delete'}:
        allowed = common | {'kind', 'sender_id', 'collection_id', 'id', 'revision', 'value'}
        if values.get('kind') not in {'profile', 'episode', 'interaction'} or not re.fullmatch(r'[a-f0-9]{64}', str(values.get('revision', ''))):
            raise ValueError('Invalid memory revision')
        if values['kind'] in {'profile', 'interaction'}:
            if not re.fullmatch(r'[0-9]{0,20}', str(values.get('sender_id', ''))):
                raise ValueError('Invalid speaker')
        elif any(not isinstance(values.get(key), str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', values[key]) for key in ('collection_id', 'id')):
            raise ValueError('Invalid memory ID')
    else:
        raise ValueError('Invalid memory operation')
    if set(values) - allowed:
        raise ValueError('Unknown memory field')
    return values


async def manage(ap, context, bot, values):
    from qunling.proactive.host import _authorized_bot, list_groups
    validate(values)
    runtime = await _authorized_bot(ap, context, bot)
    group = str(values['group_id'])
    groups = await list_groups(ap, context, bot)
    if group not in {row['group_id'] for row in groups['groups']}:
        raise MemoryResetError('invalid_group', '只能管理当前 QQ 已加入的群。', 400)
    key = group_reset.scope(runtime.execution_context, bot, group)
    # Serialize with full reset; an edit must never resurrect a purged record.
    locks = getattr(ap, '_group_memory_reset_locks', None)
    if locks is None:
        ap._group_memory_reset_locks = locks = {}
    lock = locks.setdefault(key, asyncio.Lock())
    if lock.locked():
        raise MemoryResetError('memory_busy', '本群记忆正在操作，请稍后重试。')
    async with lock:
        boundary = group_reset.boundary(key)
        if boundary and boundary['pending']:
            raise MemoryResetError('memory_busy', '整群清空尚未完成，请先重试清空。')
        from qunling import reply_preferences as preferences
        if values.get('kind') == 'interaction' and values['action'] in {'save', 'delete'}:
            value = values.get('value', {}) if values['action'] == 'save' else {'mode': 'normal', 'rule': ''}
            if not isinstance(value, dict) or set(value) != {'mode', 'rule'}:
                raise ValueError('Invalid interaction preference')
            try:
                preferences.save(key, values['sender_id'], **value, expected_revision=values['revision'],
                                 source_message_id='admin', source_time=time.time())
            except preferences.PreferenceError as exc:
                raise MemoryResetError('memory_conflict', str(exc), 409) from None
            return {'group_id': group, 'saved' if values['action'] == 'save' else 'deleted': True}
        entries = []
        if values['action'] in {'list', 'export'} and values.get('kind', 'all') in {'all', 'interaction'}:
            for row in preferences.list_group(key):
                entries.append({'kind': 'interaction', 'sender_id': row['sender_id'], 'title': '成员 ' + row['sender_id'],
                    'value': {'mode': row['mode'], 'rule': row['rule']}, 'revision': row['revision'], 'status': 'active',
                    'updated_at': datetime.fromtimestamp(row['updated_at'], timezone.utc).isoformat()})
            needle = values.get('query', '').casefold()
            entries = [row for row in entries if needle in (row['sender_id'] + row['value']['rule']).casefold()]
        offset = values.get('offset', 0)
        if values['action'] == 'list' and values.get('kind') == 'interaction':
            return {'group_id': group, 'items': entries[offset:offset + 30], 'total': len(entries),
                    'next_offset': offset + 30 if offset + 30 < len(entries) else None}
        await ap.plugin_connector.require_workspace_context(context)
        body = {**values, 'scope_key': f'{bot}:group_{group}'}
        if values['action'] == 'list' and entries:
            body['offset'] = max(0, offset - len(entries))
        result = await ap.plugin_connector.handle_page_api('langbot-team', 'LongTermMemory', 'memory_console', '/group', 'POST', body)
        if result.get('error'):
            # Never forward plugin tracebacks or credentials to the browser.
            raise MemoryResetError('memory_unavailable', '记忆服务暂时不可用，请稍后刷新。', 503)
        data = result.get('data')
        if not isinstance(data, dict):
            raise MemoryResetError('memory_unavailable', '记忆服务返回异常，请稍后刷新。', 503)
        if data.get('error'):
            messages = {'conflict': '这条记忆已变化，请重新载入后修改。', 'missing': '这条记忆已不存在，请刷新列表。',
                        'shared': '当前长期记忆跨群共享，无法作为本群记忆管理。', 'too_large': '记忆数量超过单次管理上限，请前往高级记忆控制台。',
                        'invalid': '记忆内容格式或长度不正确，请检查后重试。'}
            raise MemoryResetError('memory_' + data['error'], messages.get(data['error'], '记忆操作未完成，请刷新核对。'),
                                   409 if data['error'] in {'conflict', 'missing'} else 400)
        if values['action'] == 'list' and entries:
            data = {**data, 'items': (entries[offset:offset + 30] + data.get('items', []))[:30],
                    'total': len(entries) + data.get('total', len(data.get('items', [])))}
            data['next_offset'] = offset + 30 if offset + 30 < data['total'] else None
        elif values['action'] == 'export':
            data = {**data, 'reply_preferences': entries}
        return {**data, 'group_id': group}
