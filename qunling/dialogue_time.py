"""Explicit local time shared by the QQ conversation and the tool runner."""
from datetime import datetime
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def clock(target_timestamp=None, *, now=None):
    name = os.environ.get("TZ") or "Asia/Shanghai"
    try:
        zone = ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        name, zone = "UTC", ZoneInfo("UTC")
    current = datetime.fromtimestamp(now, zone) if now is not None else datetime.now(zone)
    value = {"timezone": name, "current_time": current.isoformat(timespec="seconds"),
             "current_date": current.strftime("%Y-%m-%d (%A)"),
             "utc_offset": current.strftime("%z")}
    if type(target_timestamp) in (int, float):
        try:
            value["target_time"] = datetime.fromtimestamp(target_timestamp, zone).isoformat(timespec="seconds")
        except (ValueError, OverflowError, OSError):
            pass
    return value
