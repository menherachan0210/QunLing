"""Record bounded routing decisions without storing chats or model prompts."""
from types import SimpleNamespace
import uuid


def record(bot, context, decision, elapsed):
    from qunling.request_journal import get_journal
    ap = getattr(bot, "ap", None)
    if ap is None:
        return
    try:
        active = bot.execution_context
        store = get_journal(bot.ap, active.instance_uuid)
        if store is None:
            return
        target = decision.get("target_message_id") or next(iter(reversed(context.reply_candidate_ids)), "")
        message = next((item for item in context.messages if item.message_id == target), None)
        if message is None:
            return
        query = SimpleNamespace(query_uuid="participation-" + uuid.uuid4().hex,
                                message_event=SimpleNamespace(message_id=target))
        owner = (active.instance_uuid, active.workspace_uuid, active.placement_generation,
                 str(bot.bot_entity.uuid), context.group_id, message.sender_id)
        key = store.start(query, owner, origin="participation")
        understanding = decision.get("understanding") or {}
        participation = understanding.get("participation") or {}
        info = {"kind": "participation", "action": decision.get("action"),
            "target_message_id": target, "elapsed_ms": elapsed * 1000,
            **{k: understanding.get(k) for k in ("response_mode", "purpose", "context_message_ids", "information_needs")},
            **{k: participation.get(k) for k in ("role", "context_sufficient", "entry_point")}}
        store.record(key, "ended", status="processed", details=info)
    except Exception:
        # Diagnostics must never interrupt or retry a chat operation.
        ap._request_diagnostics_degraded = True
