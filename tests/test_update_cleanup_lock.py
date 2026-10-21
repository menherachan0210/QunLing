"""Maintenance must exclude retention even before it publishes a busy status."""
from deploy.updater.engine import UpdateEngine
from deploy.updater.locking import worker_lock
from deploy.updater import retention


def test_worker_lock_excludes_other_cleanup_handle(tmp_path, monkeypatch):
    engine = UpdateEngine(tmp_path)
    engine.publish(phase="idle")
    calls = []
    monkeypatch.setattr(retention, "cleanup", lambda *a, **kw: calls.append(kw) or {"status": "completed"})
    with worker_lock(tmp_path) as acquired:
        assert acquired
        assert engine.cleanup_history(force=True) is None
        assert not calls
    assert engine.cleanup_history(force=True)["status"] == "completed"
    assert len(calls) == 1


def test_completed_maintenance_rechecks_without_waiting_one_hour(tmp_path, monkeypatch):
    engine = UpdateEngine(tmp_path)
    engine.publish(phase="idle", proactive_completed="first")
    calls = []
    monkeypatch.setattr(retention, "cleanup", lambda *a, **kw: calls.append(kw) or {"status": "completed"})
    engine.cleanup_history()
    engine.cleanup_history()
    assert len(calls) == 1
    engine.publish(proactive_completed="second")
    engine.cleanup_history()
    assert len(calls) == 2


def test_busy_state_is_rechecked_after_file_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from deploy.updater import locking
    engine = UpdateEngine(tmp_path)
    engine.publish(phase="idle")
    calls = []
    @contextmanager
    def maintenance_claimed(_):
        engine.publish(phase="recovery_required")
        yield True
    monkeypatch.setattr(locking, "worker_lock", maintenance_claimed)
    monkeypatch.setattr(retention, "cleanup", lambda *a, **kw: calls.append(kw))
    assert engine.cleanup_history(force=True) is None
    assert not calls
