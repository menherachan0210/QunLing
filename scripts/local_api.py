"""Authenticated access to this deployment's local LangBot management API."""

import json
from pathlib import Path
import secrets
import requests

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.local'


class LocalAPI:
    def __init__(self):
        STATE.mkdir(exist_ok=True)
        credentials = STATE / 'langbot-admin.json'
        self.http = requests.Session()
        self.http.trust_env = False
        initialized = self.call('GET', '/api/v1/user/init')['initialized']
        if not credentials.exists():
            if initialized:
                raise RuntimeError('LangBot already has an account; existing credentials were not changed.')
            credentials.write_text(json.dumps({
                'user': 'admin@local.invalid',
                'password': secrets.token_urlsafe(24),
            }, indent=2), encoding='utf-8')
        account = json.loads(credentials.read_text(encoding='utf-8'))
        if not initialized:
            self.call('POST', '/api/v1/user/init', account)
        token = self.call('POST', '/api/v1/user/auth', account)['token']
        self.http.headers['Authorization'] = 'Bearer ' + token

    def call(self, method, path, data=None):
        if not path.startswith('/api/v1/'):
            raise ValueError('Only local LangBot management endpoints are allowed')
        response = self.http.request(method, 'http://127.0.0.1:5300' + path, json=data, timeout=45)
        response.raise_for_status()
        result = response.json()
        if result.get('code') != 0:
            raise RuntimeError(f'{method} {path}: {result.get("msg", "unknown error")}')
        return result.get('data')


if __name__ == '__main__':
    api = LocalAPI()
    print('Local administrator initialized. Credentials: .local/langbot-admin.json')
