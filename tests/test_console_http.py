from unittest.mock import Mock

from test_public_setup_server import endpoint, headers
from bootstrap.api import SetupError, LangBotAPI


def test_task_endpoint_auth_query_validation_and_no_store(endpoint):
    http, url, manager = endpoint
    manager.console_tasks = Mock(return_value={'tasks':[], 'available':True})
    assert http.get(url + '/api/tasks', timeout=5).status_code == 401
    for query in ['group=1&group=2', 'status=sent', 'limit=0', 'workspace=other']:
        assert http.get(url + '/api/tasks?' + query, headers=headers(url), timeout=5).status_code == 400
    manager.console_tasks.assert_not_called()
    response = http.get(url + '/api/tasks?group=123', headers=headers(url), timeout=5)
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    assert manager.console_tasks.call_args.args[0]['group'] == '123'


def test_memory_post_requires_auth_origin_and_json_and_preserves_conflict(endpoint):
    http, url, manager = endpoint
    manager.console_memory = Mock(return_value={'group_id':'123', 'items':[]})
    body = {'group_id':'123', 'action':'list'}
    assert http.post(url + '/api/memory', json=body, timeout=5).status_code == 403
    assert http.post(url + '/api/memory', json=body, headers={**headers(url),'Origin':'http://other'}, timeout=5).status_code == 403
    manager.console_memory.assert_not_called()
    assert http.post(url + '/api/memory', json=body, headers=headers(url), timeout=5).status_code == 200
    error = SetupError('记忆已变化'); error.status = 409
    manager.console_memory.side_effect = error
    assert http.post(url + '/api/memory', json=body, headers=headers(url), timeout=5).status_code == 409


def test_plugin_error_codes_cross_proxy_without_private_details():
    api = LangBotAPI('http://localhost', Mock())
    api.http.request = Mock(return_value=Mock(status_code=409, ok=False, json=lambda:{'error_code':'memory_conflict','msg':'private traceback'}))
    try:
        api.call('POST', '/api/v1/platform/bots/bot/console/memory', {})
    except SetupError as error:
        assert error.status == 409 and '已变化' in str(error) and 'private' not in str(error)
    else:
        raise AssertionError('Expected conflict')
