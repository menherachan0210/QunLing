"""Trusted execution scopes. Model tool parameters must never create these."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


@dataclass(frozen=True)
class Scope:
    workspace_id: str
    bot_id: str
    conversation_id: str
    actor_id: str

    def __post_init__(self):
        for value in asdict(self).values():
            if not isinstance(value, str) or not value or len(value) > 256:
                raise ValueError("Incomplete or invalid trusted scope")
            if any(ord(c) < 32 for c in value):
                raise ValueError("Invalid trusted scope")

    @property
    def key(self) -> str:
        value = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, str]:
        return asdict(self)
