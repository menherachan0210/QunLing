"""Recover a flattened QQ display-name mention on a mixed-media message.

Only the current account's platform-verified names are accepted at a message
boundary. Quoted/forwarded text and configured persona aliases are never used.
The sender, group, attachment order and native message identity stay intact.
"""
from __future__ import annotations

import asyncio
import os
import time


def _replacement(parts, names, self_id):
    texts = [(i, p.get('data', {}).get('text', '')) for i, p in enumerate(parts)
             if p.get('type') == 'text']
    nonempty = [(i, text) for i, text in texts if isinstance(text, str) and text.strip()]
    if not nonempty:
        return None
    first, last = nonempty[0][0], nonempty[-1][0]
    for index, text in nonempty:
        for name in sorted(names, key=len, reverse=True):
            marker = '@' + name
            start = len(text) - len(text.lstrip())
            end = len(text.rstrip())
            if (index == first and text.startswith(marker, start)
                    and (start + len(marker) == len(text) or text[start + len(marker)].isspace())):
                offset = start
            elif (index == last and text[:end].endswith(marker)
                  and (end == len(marker) or text[end - len(marker) - 1].isspace())):
                offset = end - len(marker)
            else:
                continue
            replacement = []
            if text[:offset]:
                replacement.append({'type': 'text', 'data': {'text': text[:offset]}})
            replacement.append({'type': 'at', 'data': {'qq': self_id}})
            if text[offset + len(marker):]:
                replacement.append({'type': 'text', 'data': {'text': text[offset + len(marker):]}})
            return parts[:index] + replacement + parts[index + 1:]
    return None


async def normalize(adapter, payload):
    if (not os.environ.get('QUNLING_DATA_DIR') or payload.get('post_type') != 'message'
            or payload.get('message_type') != 'group'):
        return payload
    parts = payload.get('message')
    self_id = str(getattr(adapter, 'bot_account_id', '') or '')
    group = str(payload.get('group_id') or '')
    if (not self_id.isdecimal() or str(payload.get('self_id')) != self_id or not group.isdecimal()
            or not isinstance(parts, list) or len(parts) > 100
            or not all(isinstance(p, dict) and isinstance(p.get('data'), dict) for p in parts)
            or not any(p.get('type') in {'image', 'file'} for p in parts)
            or any(p.get('type') == 'at' for p in parts)
            or not any(p.get('type') == 'text' and '@' in str(p['data'].get('text', '')) for p in parts)):
        return payload
    client = adapter.bot
    connection = getattr(client, 'connection', None)
    state = getattr(client, '_qunling_mention_names', None)
    if state is None or state['connection'] is not connection or state['account'] != self_id:
        state = {'connection': connection, 'account': self_id, 'groups': {}, 'lock': asyncio.Lock()}
        client._qunling_mention_names = state
    async with state['lock']:
        now = time.monotonic()
        cached = state['groups'].get(group)
        if cached is None or cached[0] < now:
            try:
                async with asyncio.timeout(3):
                    member = await client.get_group_member_info(
                        group_id=int(group), user_id=int(self_id), no_cache=True)
                if (not isinstance(member, dict) or str(member.get('user_id')) != self_id
                        or str(member.get('group_id', group)) != group):
                    return payload
                names = tuple(v for k in ('card', 'nickname') if isinstance(v := member.get(k), str)
                              and 2 <= len(v) <= 100 and v == v.strip()
                              and not any(ord(c) < 32 or c in '@\r\n' for c in v))
                cached = (now + 300, names)
            except Exception:
                # A lookup failure must not delay or discard ordinary ingress.
                cached = (now + 10, ())
            if len(state['groups']) >= 128:
                state['groups'].pop(next(iter(state['groups'])))
            state['groups'][group] = cached
    updated = _replacement(parts, cached[1], self_id)
    if updated is None:
        return payload
    return {**payload, 'message': updated, '_qunling_mention_source': 'qq_display_name'}
