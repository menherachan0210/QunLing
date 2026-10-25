"""Authorized setup-page proxy; the wizard never mounts private bot data."""
from bootstrap.api import SetupError
from bootstrap.proactive import _path
from qunling.knowledge.config import ConfigError, defaults, validate


def settings(manager):
    if not manager.snapshot().get("configured"):
        return {"config": defaults(), "status": {"available": False}}
    return manager.api.call("GET", _path(manager).removesuffix("/proactive") + "/knowledge")


def save(manager, values):
    try:
        values = validate(values)
    except ConfigError as exc:
        raise SetupError(str(exc)) from None
    with manager.lock:
        if manager.update_inflight or manager.update_status()["busy"] or manager.pending:
            raise SetupError("服务正在变更，完成后再保存知识库设置。")
        return manager.api.call("POST", _path(manager).removesuffix("/proactive") + "/knowledge", values)
