import threading

import pytest
import requests

from bootstrap.access import Access
from bootstrap.server import SetupHTTPServer, main
from bootstrap.state import State
from test_public_setup_server import Manager


ORIGIN = 'https://203.0.113.8'


def test_public_entry_survives_restart_and_private_link_uses_it(tmp_path, monkeypatch, capsys):
    state = State(tmp_path)
    state.write({'origin': ORIGIN, 'trusted_proxies': ['172.19.0.1']}, 'public-access.json')
    state.write({'setup_token': 'offline-access-token'}, 'secrets.json')
    assert Access.read(State(tmp_path)).origin == ORIGIN
    monkeypatch.setenv('QQBOT_STATE_DIR', str(tmp_path))
    monkeypatch.setattr('sys.argv', ['server', '--url'])
    main()
    assert capsys.readouterr().out.strip() == ORIGIN + '/#token=offline-access-token'


@pytest.mark.parametrize('origin', ['http://203.0.113.8', 'https://user:pass@example.org',
    'https://example.org/path', 'https://example.org/?key=secret', 'https://example.org/#key',
    'https://example.org:99999', 'https://bad host', '//example.org'])
def test_rejects_unsafe_or_ambiguous_public_origins(origin):
    with pytest.raises(ValueError): Access({'origin': origin, 'trusted_proxies': []})


def test_forwarded_client_ip_is_accepted_only_from_explicit_proxy():
    access = Access({'origin': ORIGIN, 'trusted_proxies': ['172.19.0.1']})
    assert access.client('172.19.0.1', {'X-Real-IP': '198.51.100.2'}) == '198.51.100.2'
    assert access.client('198.51.100.3', {'X-Real-IP': '198.51.100.2'}) == '198.51.100.3'
    assert access.client('172.19.0.1', {'X-Real-IP': '198.51.100.2, 198.51.100.3'}) == '172.19.0.1'


def test_public_https_login_secure_cookie_origin_and_per_client_rate_limits(tmp_path):
    manager = Manager(tmp_path)
    manager.state.write({'origin': ORIGIN, 'trusted_proxies': ['127.0.0.1']}, 'public-access.json')
    server = SetupHTTPServer(('127.0.0.1', 0), manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    http = requests.Session(); http.trust_env = False
    url = f'http://127.0.0.1:{server.server_port}'
    headers = {'Host': '203.0.113.8', 'Origin': ORIGIN, 'X-Real-IP': '198.51.100.2'}
    try:
        assert http.get(url+'/api/status', headers=headers).status_code == 401
        assert http.post(url+'/api/session', json={'token':'offline-test-token'},
                         headers={**headers,'Origin':'http://203.0.113.8'}).status_code == 403
        result=http.post(url+'/api/session', json={'token':'offline-test-token'}, headers=headers)
        assert result.status_code==200 and 'Secure' in result.headers['Set-Cookie']
        for _ in range(9):
            assert http.post(url+'/api/session', json={'token':'wrong'}, headers=headers).status_code==401
        assert http.post(url+'/api/session', json={'token':'wrong'}, headers=headers).status_code==429
        assert http.post(url+'/api/session', json={'token':'offline-test-token'},
                         headers={**headers,'X-Real-IP':'198.51.100.3'}).status_code==200
        value=http.get(url+'/api/status',headers={**headers,'Authorization':'Bearer offline-test-token'}).json()
        assert value['management_url']==ORIGIN
        assert http.post(url+'/api/session', json={'token':'offline-test-token'},
                         headers={**headers,'Origin':'https://elsewhere.invalid'}).status_code==403
    finally:
        http.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)
