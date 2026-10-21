"""Dashboard health must describe current connectivity, never account history."""
from types import SimpleNamespace
import time

import pytest

from bootstrap.api import SetupError
from bootstrap.server import SetupManager, QQ_STATUS_TTL
from bootstrap.state import State, prepare


@pytest.fixture
def manager(tmp_path, monkeypatch):
    prepare(tmp_path / "private", tmp_path / "napcat")
    value = SetupManager(State(tmp_path / "private"), "http://app:5300",
                         "http://napcat:6099", tmp_path, "7zz")
    value.update(configured=True, plugins_ready=True, stage="waiting_qq")
    value.napcat = SimpleNamespace(call=lambda *_: {"isLogin": True})
    monkeypatch.setattr("bootstrap.server.link_napcat", lambda *_: False)
    return value


RECORD = {"configured": True, "onebot_url": "ws://app/bot", "bot_uuid": "fixture"}


def probe(manager, state="online", online=True, connected=True, checked=None):
    manager.api = SimpleNamespace(call=lambda *_: {"bot": {"adapter_runtime_values": {
        "bot_account_id": "previous-account", "qq_status": {
            "state": state, "online": online, "transport_connected": connected,
            "checked_at": time.time() if checked is None else checked,
        }
    }}})


def test_historical_account_id_alone_is_unknown(manager):
    manager.api = SimpleNamespace(call=lambda *_: {"bot": {"adapter_runtime_values": {
        "bot_account_id": "previous-account"}}})
    manager._qq_status(RECORD)
    result = manager.snapshot()
    assert result["qq_state"] == "unknown"
    assert not result["qq_connected"] and result["stage"] != "ready"


@pytest.mark.parametrize("state,online,connected,expected", [
    ("online", True, True, "online"),
    ("offline", False, True, "offline"),
    ("disconnected", None, False, "disconnected"),
    ("unknown", None, True, "unknown"),
    ("online", True, False, "unknown"),
    ("online", "true", True, "unknown"),
])
def test_real_channel_status_overrides_login_flag(manager, state, online, connected, expected):
    probe(manager, state, online, connected)
    manager._qq_status(RECORD)
    result = manager.snapshot()
    assert result["qq_state"] == expected
    assert result["qq_connected"] is (expected == "online")
    assert result["qq_status_fresh"]


@pytest.mark.parametrize("checked", [0, True, float("nan"), float("inf")])
def test_invalid_or_stale_probe_never_turns_green(manager, checked):
    probe(manager, checked=checked)
    manager._qq_status(RECORD)
    assert manager.snapshot()["qq_state"] == "unknown"


def test_login_offline_flag_wins_over_saved_login(manager):
    manager.napcat = SimpleNamespace(call=lambda *_: {
        "isLogin": True, "isOffline": True, "qrcodeurl": "cached-invalid-qr"})
    manager._qq_status(RECORD)
    status = manager.snapshot()
    assert status["qq_state"] == "offline"
    assert not status["qq_connected"] and not status["qq_logged_in"]
    assert not status["qr_image"]


def test_stale_snapshot_withdraws_ready_even_when_poll_thread_is_blocked(manager, monkeypatch):
    probe(manager)
    manager._qq_status(RECORD)
    sampled, checked = manager.qq_sampled_at, manager.snapshot()["qq_checked_at"]
    monkeypatch.setattr("bootstrap.server.time.monotonic", lambda: sampled + QQ_STATUS_TTL + 1)
    status = manager.snapshot()
    assert not status["qq_status_fresh"] and not status["qq_connected"]
    assert not status["qq_logged_in"] and status["qq_state"] == "unknown"
    assert status["stage"] == "waiting_qq" and status["qq_checked_at"] == checked


def test_online_offline_unknown_and_recovery(manager):
    for state, online, transport in [("online", True, True), ("offline", False, True),
                                     ("unknown", None, True), ("online", True, True)]:
        probe(manager, state, online, transport)
        manager._qq_status(RECORD)
        status = manager.snapshot()
        assert status["qq_state"] == state
        assert status["qq_connected"] is (state == "online")


def test_refresh_checks_actual_login_instead_of_cached_true(manager):
    manager._record_qq("online", True, True)
    calls = []
    def call(path):
        calls.append(path)
        return {"isLogin": False}
    manager.napcat = SimpleNamespace(call=call)
    manager.refresh_qr()
    assert calls == ["/QQLogin/CheckLoginStatus", "/QQLogin/RefreshQRcode"]
    assert manager.snapshot()["qq_state"] == "waiting_login"
    assert not manager.snapshot()["qq_connected"]


def test_refresh_does_not_interrupt_actual_online_account(manager):
    calls = []
    def call(path):
        calls.append(path)
        return {"isLogin": True, "isOffline": False}
    manager.napcat = SimpleNamespace(call=call)
    manager.refresh_qr()
    assert calls == ["/QQLogin/CheckLoginStatus"]


@pytest.mark.parametrize("stage", ["error", "configuring"])
def test_expiration_and_probe_error_preserve_configuration_state(manager, stage):
    manager.update(stage=stage, message="configuration-message")
    manager._record_qq("unknown")
    manager.qq_sampled_at -= QQ_STATUS_TTL + 1
    status = manager.snapshot()
    assert status["stage"] == stage and status["message"] == "configuration-message"
