"""Cookie lifecycle, CSRF boundary and production asset delivery."""
import hashlib
import json
import re
import time

from bootstrap.sessions import Sessions, cookie
from bootstrap.state import State
from test_public_setup_server import endpoint


def test_cookie_login_refresh_logout_and_csrf(endpoint):
    http, url, manager = endpoint
    assert http.post(url+'/api/session', json={'token':'offline-test-token'}).status_code == 403
    assert http.get(url+'/api/session').json() == {'authenticated':False}
    result = http.post(url+'/api/session',json={'token':'offline-test-token'},headers={'Origin':url})
    assert result.status_code == 200
    assert 'HttpOnly' in result.headers['Set-Cookie'] and 'SameSite=Strict' in result.headers['Set-Cookie']
    assert http.get(url+'/api/status').status_code == 200
    assert http.post(url+'/api/configure',json={},headers={'Origin':'https://untrusted.invalid'}).status_code == 403
    assert manager.submissions == []
    stored=json.dumps(manager.state.read('console-sessions.json'))
    assert http.cookies['qunling_session'] not in stored
    assert 'offline-test-token' not in stored
    assert http.post(url+'/api/session/logout',json={},headers={'Origin':url}).status_code == 200
    assert http.get(url+'/api/status').status_code == 401


def test_session_restart_rotation_expiry_and_bounded_history(tmp_path):
    state=State(tmp_path)
    sessions=Sessions(state,'owner')
    token=sessions.issue()
    header=cookie(token)
    assert Sessions(state,'owner').valid(header)
    assert not Sessions(state,'rotated-owner').valid(header)
    for _ in range(17): sessions.issue()
    assert len(state.read('console-sessions.json')['sessions']) == 16
    assert not sessions.valid(header)
    current=sessions.issue()
    value=state.read('console-sessions.json')
    value['sessions'][hashlib.sha256(current.encode()).hexdigest()]=time.time()-1
    state.write(value,'console-sessions.json')
    assert not sessions.valid(cookie(current))
    state.write({'owner':sessions.owner,'sessions':[]},'console-sessions.json')
    assert not sessions.valid(cookie(current))


def test_failed_login_is_rate_limited(endpoint):
    http,url,_=endpoint
    for _ in range(10):
        assert http.post(url+'/api/session',json={'token':'wrong'},headers={'Origin':url}).status_code == 401
    assert http.post(url+'/api/session',json={'token':'offline-test-token'},headers={'Origin':url}).status_code == 429


def test_built_assets_have_types_cache_policy_and_no_traversal(endpoint):
    http,url,_=endpoint
    page=http.get(url)
    scripts=re.findall(r'(?:src|href)="(/assets/[^\"]+)"',page.text)
    assert scripts, 'Run npm ci && npm run build in frontend before testing.'
    for path in scripts:
        response=http.get(url+path)
        assert response.status_code==200 and 'immutable' in response.headers['Cache-Control']
        assert ('javascript' if path.endswith('.js') else 'text/css') in response.headers['Content-Type']
    for path in ('/assets/%2e%2e/server.py','/assets/../server.py','/assets/..%5csessions.py'):
        assert http.get(url+path).status_code in (401,404)
    notices=http.get(url+'/licenses.txt')
    assert notices.status_code==200 and 'text/plain' in notices.headers['Content-Type']
    assert 'React' in notices.text or 'react' in notices.text


def test_configure_acknowledges_exact_target_revision(endpoint):
    http,url,manager=endpoint
    manager.submit=lambda value:'a'*64
    response=http.post(url+'/api/configure',json={},headers={'Origin':url,'Authorization':'Bearer offline-test-token'})
    assert response.status_code==202
    assert response.json()=={'status':'accepted','target_revision':'a'*64}
