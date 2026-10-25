"""Suppress only duplicate automatic recall, never knowledge-base authorization."""
from copy import deepcopy
import hashlib

MARKER = "_qunling_memory_auto_recall"
RUNNER = "plugin:langbot-team/LocalAgent/default"


def completed(query_id, query_uuid, kb_id, text):
    return {"query_id": query_id, "query_uuid": query_uuid, "kb_id": kb_id,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest()}


def deduplicate(query, context, runner_id):
    if runner_id != RUNNER:
        return False
    variables = getattr(query, "variables", {}) or {}
    marker = variables.get(MARKER)
    text = variables.get("user_message_text")
    if not isinstance(marker, dict) or not isinstance(text, str) or not text:
        return False
    expected = completed(query.query_id, getattr(query, "query_uuid", None), marker.get("kb_id"), text)
    if marker != expected or not isinstance(marker.get("kb_id"), str):
        return False
    config = context.get("config", {})
    selected = config.get("knowledge-bases", [])
    if not isinstance(selected, list) or marker["kb_id"] not in selected:
        return False
    # The resource envelope has already been built from the original binding.
    # Copy only the per-run automatic-RAG selector, leaving tools authorized.
    context["config"] = deepcopy(config)
    context["config"]["knowledge-bases"] = [kb for kb in selected if kb != marker["kb_id"]]
    return True
