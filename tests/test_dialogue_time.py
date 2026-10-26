from datetime import datetime, timezone

from qunling.dialogue_time import clock


def test_local_midnight_and_event_time_remain_distinct(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    now = datetime(2026, 10, 26, 17, 0, tzinfo=timezone.utc).timestamp()
    result = clock(now - 7200, now=now)
    assert result["current_date"].startswith("2026-10-27")
    assert result["current_time"] == "2026-10-27T01:00:00+08:00"
    assert result["target_time"] == "2026-10-26T23:00:00+08:00"


def test_configured_zone_and_invalid_zone_have_explicit_offsets(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    assert clock(now=0)["current_time"] == "1969-12-31T19:00:00-05:00"
    monkeypatch.setenv("TZ", "invalid-zone")
    assert clock(float("nan"), now=0) == {"timezone": "UTC", "current_time": "1970-01-01T00:00:00+00:00",
        "current_date": "1970-01-01 (Thursday)", "utc_offset": "+0000"}
