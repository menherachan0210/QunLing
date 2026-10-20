"""Bind shared tools to Host-supplied message identity, never model arguments."""
from __future__ import annotations

import hashlib
import json
import secrets
import time

from qunling.assets import AssetError
from qunling.scope import Scope

CONTEXT_KEY = "qunling.context.v1"
CONTEXT_TTL = 900


def workspace_key(plugin) -> str:
    getter = getattr(plugin, "get_installation_binding", None)
    binding = getter() if callable(getter) else None
    if binding is not None:
        values = [getattr(binding, k, None) for k in ("instance_uuid", "workspace_uuid")]
        if not all(isinstance(v, str) and v for v in values):
            raise AssetError("scope_unavailable", "无法确认文件所属工作空间。")
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()
    # Dedicated legacy runtimes must configure one Host-provided shared identity.
    # Never silently share data among unbound plugin instances.
    value = getattr(plugin, "_qunling_workspace", None)
    if value is None:
        value = secrets.token_hex(32)
        plugin._qunling_workspace = value
    return value


def source_scope(plugin, source) -> Scope:
    getter = getattr(plugin, "get_installation_binding", None)
    binding = getter() if callable(getter) else None
    if binding is not None:
        for name in ("instance_uuid", "workspace_uuid", "placement_generation"):
            supplied = getattr(source, name, None)
            if supplied is not None and supplied != getattr(binding, name, None):
                raise AssetError("scope_mismatch", "附件来源不属于当前运行实例。")
    kind = getattr(source.launcher_type, "value", source.launcher_type)
    if kind not in {"group", "person"}:
        raise AssetError("scope_unavailable", "无法确认文件所属会话。")
    if any(getattr(source, key, None) in (None, "") for key in ("bot_uuid", "launcher_id", "sender_id")):
        raise AssetError("scope_unavailable", "无法确认文件所属会话。")
    return Scope(
        workspace_key(plugin), str(source.bot_uuid),
        f"{kind}:{source.launcher_id}", str(source.sender_id),
    )


def descriptor(plugin, source, query_uuid: str | None, *, inbox_refs=(), unresolved=False, captured_at=None) -> dict:
    getter = getattr(plugin, "get_installation_binding", None)
    binding = getter() if callable(getter) else None
    route = {
        key: getattr(binding, key, None) for key in ("instance_uuid", "workspace_uuid", "placement_generation")
    } if binding is not None else None
    return {
        "version": 1, "scope": source_scope(plugin, source).as_dict(),
        "query_uuid": query_uuid, "created": time.time() if captured_at is None else captured_at,
        "request_key": query_uuid or secrets.token_hex(24),
        "inbox_refs": list(inbox_refs), "unresolved_quote": bool(unresolved),
        "host_route": route,
    }


async def query_context(plugin, api) -> dict:
    variables = await api.get_query_vars()
    value = variables.get(CONTEXT_KEY) if isinstance(variables, dict) else None
    try:
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError
        scope = Scope(**value["scope"])
        if scope.workspace_id != workspace_key(plugin):
            raise ValueError
        if value["query_uuid"] != getattr(api, "query_uuid", None):
            raise ValueError
        if not 0 <= time.time() - value["created"] < CONTEXT_TTL:
            raise ValueError
        if str(await api.get_bot_uuid()) != scope.bot_id:
            raise ValueError
        return value
    except (KeyError, TypeError, ValueError):
        raise AssetError("scope_unavailable", "本轮文件上下文已失效，请重新提出任务。") from None


async def query_scope(plugin, api) -> Scope:
    return Scope(**(await query_context(plugin, api))["scope"])


def task_hint(*, status="pending", can_continue=True, retryable=False, error_code=None,
              next_action="continue", task_id=None):
    from qunling.tasks.hints import task_hint as normalized_hint
    return normalized_hint(
        status=status, can_continue=can_continue, retryable=retryable,
        error_code=error_code, next_action=next_action, task_id=task_id,
    )
