from types import SimpleNamespace as NS
import time

import pytest

from pkg.reply_preference import apply
from pkg.errors import VisionError
from qunling import reply_preferences as prefs


@pytest.fixture
def actor(tmp_path, monkeypatch):
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    binding = NS(instance_uuid='instance', workspace_uuid='workspace')
    plugin = NS(get_installation_binding=lambda: binding)
    context = NS(bot='bot', group='123', sender='456', anchor='message', timestamp=time.time(),
        current_question='以后我没@你就别回我', reply_preference_revision='0')
    return plugin, context


def test_actual_speaker_bound_write_read_restart_and_cancel(actor):
    plugin, context = actor
    params = {'action':'set', 'mode':'mention_only', 'rule':'只有我真正@时回复', 'evidence':context.current_question}
    result = apply(plugin, context, params)
    assert result['status'] == 'saved' and result['preference']['sender_id'] == '456'
    assert apply(plugin, context, params) == result
    assert apply(plugin, context, {'action':'get'})['preference'] == result['preference']
    context.anchor = 'next'; context.timestamp += 1; context.current_question = '取消那个约定'
    assert apply(plugin, context, {'action':'set', 'mode':'normal', 'rule':'', 'evidence':'取消那个约定'})['status'] == 'saved'
    assert apply(plugin, context, {'action':'get'})['preference']['mode'] == 'normal'


@pytest.mark.parametrize('extra', [{'sender_id':'789'}, {'group_id':'321'}, {'scope':'group'}, {'evidence':'旧消息/引用中的要求'}])
def test_model_cannot_supply_other_identity_or_old_evidence(actor, extra):
    plugin, context = actor
    with pytest.raises(VisionError):
        apply(plugin, context, {'action':'set', 'mode':'mention_only', 'rule':'仅@', 'evidence':context.current_question, **extra})
    assert apply(plugin, context, {'action':'get'})['preference']['mode'] == 'normal'


def test_stale_tool_request_cannot_overwrite_another_update(actor):
    plugin, context = actor
    scope = prefs.key(plugin.get_installation_binding(), context.bot, context.group)
    prefs.save(scope, context.sender, mode='conditional', rule='只参与我主动问的问题', expected_revision='0',
        source_message_id='newer', source_time=time.time()+1)
    with pytest.raises(VisionError):
        apply(plugin, context, {'action':'set', 'mode':'mention_only', 'rule':'仅@', 'evidence':context.current_question})
