"""QQ member conversation lanes; platform routing and attachment scopes stay intact.

Only trusted host Query identity is used. The internal lane key is never sent as
a QQ group ID, and runner variables cannot select another member's history.
"""

from __future__ import annotations

import hashlib
import json
import uuid


def _identity(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ValueError(f"QQ {name} is required for member session isolation")
    return str(value)


def member_key(query) -> str | None:
    """Recognize the host's OneBot adapter, never user-supplied query variables."""
    launcher_type = getattr(query, "launcher_type", None)
    if getattr(launcher_type, "value", launcher_type) != "group":
        return None
    adapter = getattr(query, "adapter", None)
    runtime = getattr(adapter, "_runtime", None)
    entity = getattr(runtime, "bot_entity", None)
    adapter_name = getattr(entity, "adapter", None)
    cls = type(adapter)
    canonical = adapter_name == "onebot-v11"
    legacy = cls.__name__ == "AiocqhttpAdapter" and cls.__module__ in (
        "langbot.pkg.platform.adapters.aiocqhttp.adapter",
        "langbot.pkg.platform.sources.aiocqhttp",
    )
    if not canonical and not legacy:
        return None
    values = [
        "qq-member", _identity(query.launcher_id, "group"),
        _identity(query.sender_id, "sender"),
    ]
    from qunling.group_reset import for_context
    state = for_context(getattr(runtime, "execution_context", None),
                        str(getattr(query, "bot_uuid", "")), str(query.launcher_id))
    if state:
        if state["pending"]:
            raise RuntimeError("Group memory reset is pending")
        values.append(state["generation"])
    return json.dumps(values, ensure_ascii=True, separators=(",", ":"))


def session_key_id(query):
    """Keep the upstream six-part cache index; change only its internal final key."""
    return member_key(query) or query.launcher_id


def _validate_session(query, session):
    if (str(session.launcher_id), str(session.sender_id)) != (str(query.launcher_id), str(query.sender_id)):
        raise ValueError("QQ runner session does not belong to this member")
    if getattr(session, "bot_uuid", None) != getattr(query, "bot_uuid", None):
        raise ValueError("QQ runner session does not belong to this bot")
    scope = getattr(query, "_execution_context", None)
    session_scope = getattr(session, "_execution_context", None)
    for field in ("instance_uuid", "workspace_uuid", "placement_generation"):
        if getattr(scope, field, None) != getattr(session_scope, field, None):
            raise ValueError("QQ runner session does not belong to this execution scope")


def bind_conversation(query, session, conversation, *, renew=False):
    key = member_key(query)
    if key is None:
        return
    _validate_session(query, session)
    previous = getattr(conversation, "_qunling_member_key", None)
    if previous is not None and previous != key:
        raise ValueError("QQ conversation member identity changed")
    # Conversation.uuid belongs to external runners such as Dify. Maintain a
    # separate host ID for transcripts/state instead of overwriting that value.
    if renew or not getattr(conversation, "_qunling_conversation_id", None):
        object.__setattr__(conversation, "_qunling_conversation_id", str(uuid.uuid4()))
        object.__setattr__(conversation, "_qunling_member_key", key)


def history_conversation_id(conversation):
    host_id = getattr(conversation, "_qunling_conversation_id", None)
    return host_id if isinstance(host_id, str) and host_id else getattr(conversation, "uuid", None)


def assert_group_reset_idle(ap, workspace, bot, conversation):
    """The management reset targets a whole group, including every member lane."""
    manager = getattr(ap, "sess_mgr", None)
    if not conversation.startswith("group_") or manager is None:
        return
    group = conversation.removeprefix("group_")
    for session in manager.session_list:
        key = getattr(session, "_langbot_session_key", ())
        if (isinstance(key, tuple) and len(key) == 6 and isinstance(key[5], str) and key[5].startswith('["qq-member",')
                and key[1] == workspace and key[3] == bot
                and str(session.launcher_id) == group and key[4] == "group"
                and session._semaphore.locked()):
            raise RuntimeError("Session is processing a task; wait until it finishes")


def runner_conversation_id(query) -> str | None:
    """Resolve member history even before preprocessing, without a group fallback."""
    key = member_key(query)
    if key is None:
        return None
    session = getattr(query, "session", None)
    if session is not None:
        _validate_session(query, session)
        conversation = getattr(session, "using_conversation", None)
        if conversation is not None:
            bind_conversation(query, session, conversation)
            return history_conversation_id(conversation)
    scope = getattr(query, "_execution_context", None)
    identity = [
        _identity(getattr(scope, "instance_uuid", None), "instance"),
        _identity(getattr(scope, "workspace_uuid", None), "workspace"),
        getattr(scope, "placement_generation", None),
        _identity(getattr(query, "bot_uuid", None), "bot"),
        key, getattr(query, "pipeline_uuid", None),
    ]
    return "qq-member:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
