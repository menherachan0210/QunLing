"""Resolve voice delivery and spelling from the current chat's trusted settings."""
from __future__ import annotations

from qunling.group_persona import builtin_style, group_persona_prompt
from qunling.proactive.config import DEFAULT_GROUP


def delivery(settings, group_id=''):
    if group_id:
        policy = settings.get('groups', {}).get(str(group_id), DEFAULT_GROUP)
        return group_persona_prompt(policy.get('display_name', DEFAULT_GROUP['display_name']),
            policy.get('persona', ''), settings.get('default_style', ''),
            examples=policy.get('examples', ()), default_examples=settings.get('default_examples', ()),
            include_examples=False)
    # A private conversation has no group. Never borrow another group's persona.
    return settings.get('default_style', '').strip() or builtin_style()


def spelling(settings, event):
    group = getattr(event, 'group', None)
    group_id = str(getattr(group, 'id', '') or '')
    policy = settings.get('groups', {}).get(group_id, DEFAULT_GROUP) if group_id else {}
    values = [policy.get('display_name', ''), *policy.get('aliases', ()),
              getattr(group, 'name', ''), getattr(getattr(event, 'sender_member', None), 'display_name', ''),
              getattr(getattr(event, 'sender', None), 'nickname', '')]
    # Names are recognition vocabulary, never instructions or another group's data.
    return tuple(dict.fromkeys(v.strip()[:80] for v in values if isinstance(v, str)
        and v.strip() and not any(c in v for c in '<>\r\n') and not v.strip().isdecimal()))
