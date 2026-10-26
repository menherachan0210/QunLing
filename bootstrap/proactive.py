"""Wizard proxy for bot-scoped, authenticated participation settings."""
from __future__ import annotations

import re

from bootstrap.api import SetupError
from qunling.proactive.config import ConfigError, defaults, validate


def _path(manager):
    record = manager.state.read(default={})
    bot_uuid = record.get("bot_uuid")
    if not isinstance(bot_uuid, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", bot_uuid):
        raise SetupError("请先完成 AI 配置并连接 QQ，再设置自由对话。")
    return "/api/v1/platform/bots/" + bot_uuid + "/proactive"


def settings(manager):
    if not manager.snapshot().get("configured"):
        return {"config": defaults(), "status": {"available": False,
                "message": "请先完成 AI 配置并连接 QQ。"}}
    return manager.api.call("GET", _path(manager))


def groups(manager):
    if not manager.snapshot().get("qq_logged_in"):
        raise SetupError("QQ 尚未登录，登录后才能读取群列表。")
    return manager.api.call("GET", _path(manager) + "/groups")


def save(manager, values):
    try:
        values = validate(values)
    except ConfigError as exc:
        raise SetupError(str(exc)) from None
    # This lock also serializes setup and update admission. The host performs
    # the authoritative membership check and revision compare-and-swap.
    with manager.lock:
        if manager.update_inflight or manager.update_status()["busy"]:
            raise SetupError("版本更新正在进行，完成后再保存自由对话设置。")
        status = manager.snapshot()
        if (manager.pending or not status.get("configured")
                or not status.get("plugins_ready") or status.get("stage") != "ready"):
            raise SetupError("机器人尚未准备好，请完成配置后重试。")
        if not status.get("qq_logged_in") or not status.get("qq_connected"):
            raise SetupError("QQ 尚未连接，请登录后再保存自由对话设置。")
        return manager.api.call("POST", _path(manager), values)


def clear_memory(manager, values):
    from qunling.group_reset_host import validate, MemoryResetError
    try:
        validate(values)
    except MemoryResetError as exc:
        raise SetupError(str(exc)) from None
    with manager.lock:
        if manager.update_inflight or manager.update_status()["busy"]:
            raise SetupError("版本更新正在进行，完成后再清空记忆。")
        status = manager.snapshot()
        if (manager.pending or not status.get("configured") or not status.get("plugins_ready")
                or status.get("stage") != "ready" or not status.get("qq_connected") or not status.get("qq_logged_in")):
            raise SetupError("机器人尚未准备好，请连接 QQ 后重试。")
        return manager.api.call("POST", _path(manager) + "/memory/reset", values)
