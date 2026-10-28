"""Opaque, revocable console sessions. Only token hashes are stored on disk."""
from __future__ import annotations

import hashlib
from http.cookies import SimpleCookie, CookieError
import secrets
import threading
import time

COOKIE = "qunling_session"
LIFETIME = 30 * 86400


class Sessions:
    def __init__(self, state, bootstrap_token):
        self.state = state
        self.owner = hashlib.sha256(bootstrap_token.encode()).hexdigest()
        self.lock = threading.RLock()
        self.attempts = {}

    def _read(self):
        try:
            value = self.state.read("console-sessions.json", {})
            if value.get("owner") != self.owner:
                return {}
            return {key: expiry for key, expiry in value.get("sessions", {}).items()
                    if isinstance(expiry, (int, float)) and expiry > time.time()}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _write(self, sessions):
        self.state.write({"owner": self.owner, "sessions": sessions}, "console-sessions.json")

    @staticmethod
    def key(header):
        try:
            cookie = SimpleCookie()
            cookie.load(header or "")
            value = cookie.get(COOKIE)
            if value is not None and len(value.value) == 64:
                return hashlib.sha256(value.value.encode()).hexdigest()
        except (CookieError, ValueError):
            pass
        return None

    def valid(self, header):
        with self.lock:
            return self.key(header) in self._read()

    def allow_attempt(self, peer):
        with self.lock:
            now = time.monotonic()
            self.attempts = {key: values for key, values in self.attempts.items() if values[-1] > now - 60}
            values = [stamp for stamp in self.attempts.get(peer, []) if stamp > now - 60]
            if len(values) >= 10 or len(self.attempts) >= 1024 and peer not in self.attempts:
                return False
            self.attempts[peer] = values + [now]
            return True

    def issue(self, previous=None):
        with self.lock:
            sessions = self._read()
            sessions.pop(self.key(previous), None)
            while len(sessions) >= 16:
                sessions.pop(min(sessions, key=sessions.get))
            value = secrets.token_hex(32)
            sessions[hashlib.sha256(value.encode()).hexdigest()] = time.time() + LIFETIME
            self._write(sessions)
            return value

    def revoke(self, header):
        with self.lock:
            sessions = self._read()
            sessions.pop(self.key(header), None)
            self._write(sessions)


def cookie(value, *, secure=False, clear=False):
    return (f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={0 if clear else LIFETIME}"
            + ("; Secure" if secure else ""))
