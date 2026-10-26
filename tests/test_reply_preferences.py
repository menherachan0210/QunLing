import time
from types import SimpleNamespace as NS

import pytest

from qunling import reply_preferences as prefs, group_reset


@pytest.fixture
def scope(tmp_path, monkeypatch):
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    return prefs.key(NS(instance_uuid='instance', workspace_uuid='workspace'), 'bot', '123')


def save(scope, **kw):
    args = dict(mode='mention_only', rule='没真正@时别接我的话', expected_revision='0',
                source_message_id='100', source_time=time.time(), evidence='别接我的话', text='以后没@别接我的话')
    args.update(kw)
    return prefs.save(scope, '456', **args)


def test_restart_identity_and_scope_isolation(scope):
    assert prefs.get(scope, '456')['mode'] == 'normal'
    row = save(scope)
    assert prefs.get(scope, '456') == row
    assert prefs.list_group(scope) == [row]
    for key, sender in [(scope, '789'), ((*scope[:2], '999'), '456'), ((scope[0], 'other-bot', scope[2]), '456'),
                        (('other-workspace', *scope[1:]), '456')]:
        assert prefs.get(key, sender)['mode'] == 'normal'
    assert not prefs.permits(row)
    assert prefs.permits(row, actual_mention=True)
    assert not prefs.permits(row, acknowledgement='different')
    assert prefs.permits(row, acknowledgement='100')


def test_stale_writes_cancellation_and_idempotency(scope):
    row = save(scope)
    assert save(scope) == row
    with pytest.raises(prefs.PreferenceError, match='已被更新'):
        save(scope, mode='normal', rule='', source_message_id='101')
    cleared = save(scope, mode='normal', rule='', expected_revision=row['revision'], source_message_id='102')
    assert cleared['revision'] != row['revision'] and prefs.list_group(scope) == []
    with pytest.raises(prefs.PreferenceError):
        save(scope)
    with pytest.raises(prefs.PreferenceError, match='较早'):
        save(scope, expected_revision=cleared['revision'], source_time=1)


@pytest.mark.parametrize('change', [dict(mode='other'), dict(rule=''), dict(mode='normal'),
    dict(rule='a'*601), dict(rule='a\x00'), dict(expected_revision=None), dict(source_time=float('nan')),
    dict(evidence='别人的话'), dict(evidence=''), dict(source_message_id='')])
def test_invalid_changes_cannot_mutate(scope, change):
    with pytest.raises(prefs.PreferenceError):
        save(scope, **change)
    assert prefs.list_group(scope) == []


def test_reset_fence_erases_agreements_and_rejects_older_inflight_write(scope):
    old = save(scope)
    group_reset.begin(scope)
    with pytest.raises(prefs.PreferenceError, match='清空'):
        save(scope, expected_revision=old['revision'], source_message_id='101')
    prefs.clear_group(scope)
    cutoff = group_reset.finish(scope, 'reset-1')
    assert prefs.get(scope, '456')['revision'] == '0'
    with pytest.raises(prefs.PreferenceError, match='清空'):
        save(scope, source_time=cutoff - 1)
    assert save(scope, source_time=time.time() + .01)['mode'] == 'mention_only'
