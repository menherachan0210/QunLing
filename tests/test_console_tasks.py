from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import json

import pytest

from qunling.console_tasks import listing, parse_filters, tasks
from qunling.scope import Scope
from qunling.tasks.store import TaskStore


@pytest.mark.parametrize('values', [{'group':'../1'}, {'status':'sent'}, {'limit':101}, {'limit':0},
    {'before':'nan:id'}, {'task_id':'../id'}, {'workspace':'other'}])
def test_invalid_filters_rejected(values):
    with pytest.raises(ValueError):
        parse_filters(values)


def test_task_scopes_pagination_no_payloads_and_separate_delivery(tmp_path):
    path = tmp_path / 'tasks.sqlite'
    store = TaskStore(path, clock=lambda: 1234)
    owners = [Scope('workspace', 'bot', 'group:123', sender) for sender in ['1', '2']]
    owners += [Scope('other', 'bot', 'group:123', '1'), Scope('workspace', 'other', 'group:123', '1'), Scope('workspace', 'bot', 'group:456', '1')]
    rows = [store.create(scope, 'processing', 'secret-goal', 'request') for scope in owners]
    with store._db() as db:
        db.execute("UPDATE ql_tasks SET status='completed',checkpoint=? WHERE id=?", (json.dumps({'stage':'processed','data':{'passwordish':'private'}}), rows[0].id))
        db.execute('CREATE TABLE ql_file_outbox(task_id TEXT, scope_key TEXT, state TEXT, error_code TEXT, created REAL, updated REAL, id TEXT)')
        db.execute('INSERT INTO ql_file_outbox VALUES(?,?,?,?,?,?,?)', (rows[0].id, owners[0].key, 'unconfirmed', 'delivery_unconfirmed', 1235, 1235, 'd'))
    result = listing(path, 'workspace', 'bot', parse_filters({'group':'123','limit':'1'}))
    assert result['counts'] == {'pending':1,'completed':1}
    second = listing(path, 'workspace', 'bot', parse_filters({'group':'123','limit':1,'before':result['next_before']}))
    all_rows = result['tasks'] + second['tasks']
    assert {row['id'] for row in all_rows} == {row.id for row in rows[:2]}
    assert next(row for row in all_rows if row['id'] == rows[0].id)['delivery'] == 'unconfirmed'
    assert 'secret-goal' not in json.dumps(result) and 'private' not in json.dumps(result)
    assert not listing(path, 'other-workspace', 'bot', parse_filters({}))['tasks']


def test_missing_store_read_does_not_create_database(tmp_path):
    path = tmp_path / 'tasks.sqlite'
    assert listing(path, 'workspace', 'bot', parse_filters({}))['tasks'] == []
    assert not path.exists()


@pytest.mark.asyncio
async def test_host_requires_resource_permission_before_read(monkeypatch):
    from langbot.pkg.api.http import authz
    def denied(*args):
        raise PermissionError('no permission')
    monkeypatch.setattr(authz, 'require_permission', denied)
    ap = NS(bot_service=NS(get_bot=AsyncMock()))
    with pytest.raises(PermissionError):
        await tasks(ap, NS(workspace_uuid='workspace'), 'bot', {})
    ap.bot_service.get_bot.assert_not_called()
