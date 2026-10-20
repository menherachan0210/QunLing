"""Versioned completion hints from trusted tool implementations, never documents."""
from __future__ import annotations

ERROR_ALIASES = {
    "network_timeout": "timeout", "fetch_timeout": "timeout", "operation_timeout": "timeout",
    "search_timeout": "timeout", "connection_failed": "network", "network_error": "network",
    "http_5xx": "service", "http_server_error": "service", "search_service_error": "service",
    "rate_limited": "rate_limit", "search_rate_or_quota_limit": "rate_limit",
    "tool_budget_exhausted": "budget", "access_denied": "permission",
    "search_authentication_failed": "permission", "send_unconfirmed": "delivery_unconfirmed",
}
ERROR_CODES = {
    "network", "timeout", "rate_limit", "service", "invalid_input", "permission", "needs_input",
    "delivery_unconfirmed", "budget", "cancelled", "internal",
}
STOP_CODES = {"permission", "needs_input", "delivery_unconfirmed", "budget", "cancelled"}
TRANSIENT_CODES = {"network", "timeout", "rate_limit", "service"}
NEXT_ACTIONS = {"retry_read", "continue", "deliver", "none"}
STATES = {"pending", "running", "waiting", "partial", "completed", "failed", "cancelled"}


def normalize_error_code(value: str | None) -> str | None:
    if not value:
        return None
    if not isinstance(value, str):
        return "internal"
    value = ERROR_ALIASES.get(value, value)
    return value if value in ERROR_CODES else "internal"


def task_hint(*, status="pending", can_continue=True, retryable=False,
              error_code=None, next_action="continue", task_id=None) -> dict:
    if status not in STATES or next_action not in NEXT_ACTIONS:
        raise ValueError("Invalid task hint")
    if type(can_continue) is not bool or type(retryable) is not bool:
        raise ValueError("Invalid task hint flags")
    code = normalize_error_code(error_code)
    retryable = retryable and code in TRANSIENT_CODES
    if code in STOP_CODES or status in {"completed", "failed", "cancelled"}:
        can_continue = False
        next_action = "none"
    if next_action == "retry_read" and not retryable:
        next_action = "continue" if can_continue else "none"
    result = {
        "version": 1, "status": status, "can_continue": can_continue,
        "retryable": retryable, "error_code": code, "next_action": next_action,
    }
    if task_id is not None:
        if not isinstance(task_id, str) or not 1 <= len(task_id) <= 128 or not task_id.isascii():
            raise ValueError("Invalid task hint identifier")
        result["task_id"] = task_id
    return result
