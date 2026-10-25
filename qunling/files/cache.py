"""Per-worker parsed document exchange; never persists passwords or archive plaintext."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
import hashlib
import json

_exchange = ContextVar("parsed_document", default=None)
VERSION = "1"  # Bump whenever parsing semantics or pinned parser dependencies change.


def fingerprint(raw, fmt, limits):
    metadata = json.dumps([VERSION, fmt, asdict(limits)], sort_keys=True).encode()
    return hashlib.sha256(metadata + raw).hexdigest()


@contextmanager
def exchange(candidate=None):
    state = {"candidate": candidate, "result": None, "hit": False}
    token = _exchange.set(state)
    try:
        yield state
    finally:
        _exchange.reset(token)


def parse_cached(raw, fmt, limits, archive, *, password=None):
    from .formats import _checked, parse
    state = _exchange.get()
    # Detect/unlock and archive member extraction remain mandatory. A secret
    # never becomes a cache key, and decrypted content is never persisted here.
    if state is None or password is not None or fmt not in {"pdf", "docx", "xlsx", "pptx"}:
        return parse(raw, fmt, limits, archive, password=password)
    key = fingerprint(raw, fmt, limits)
    cached = state["candidate"]
    if isinstance(cached, dict) and cached.get("key") == key:
        sections, warnings = cached.get("sections"), cached.get("warnings")
        if isinstance(sections, list) and isinstance(warnings, list):
            state["hit"] = True
            return _checked(sections, limits), warnings
    sections, warnings = parse(raw, fmt, limits, archive, password=password)
    state["result"] = {"key": key, "sections": sections, "warnings": warnings}
    return sections, warnings
