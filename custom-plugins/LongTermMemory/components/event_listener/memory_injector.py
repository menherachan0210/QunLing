# Modified by qq-ai-bot contributors on 2026-10-20; local changes, see NOTICE.
from __future__ import annotations

import logging
import re

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import events, context
from langbot_plugin.api.entities.builtin.provider.message import Message
from langbot_plugin.api.proxies.query_based_api import QueryBasedAPIProxy

logger = logging.getLogger(__name__)


class MemoryInjector(EventListener):
    """L1 profile + L2 episodic memory injector.

    During PromptPreProcessing:
    - L1 core profile is injected into the system prompt (default_prompt).
    - L2 episodic memory is retrieved from the memory KB and injected into
      the conversation context (prompt) so the LLM has automatic recall.
    - The Host's ``_knowledge_base_uuids`` authorization scope is preserved
      so automatic recall and memory tools retain access to the configured KB.
    """

    def __init__(self):
        super().__init__()

        @self.handler(events.PromptPreProcessing)
        async def on_prompt_preprocess(event_ctx: context.EventContext):
            try:
                await self._inject_profile(event_ctx)
            except Exception:
                logger.exception("Failed to inject profile")

        @self.handler(events.NormalMessageResponded)
        async def on_normal_message_responded(event_ctx: context.EventContext):
            try:
                await self._extract_memory_candidates(event_ctx)
            except Exception:
                logger.exception("Failed to extract memory candidates")

    @staticmethod
    def _resolve_auto_recall_top_k(config: dict) -> int:
        raw_value = config.get("auto_recall_top_k", 3)
        try:
            top_k = int(raw_value)
        except (TypeError, ValueError):
            return 3
        return max(1, top_k)

    async def _inject_profile(self, event_ctx: context.EventContext) -> None:
        store = self.plugin.memory_store
        session_name: str = event_ctx.event.session_name
        logger.info(
            "[LongTermMemory] memory injection started: query_id=%s session_name=%s",
            event_ctx.query_id,
            session_name,
        )
        api = QueryBasedAPIProxy(
            query_id=event_ctx.query_id,
            query_uuid=getattr(event_ctx, "query_uuid", None),
            plugin_runtime_handler=self.plugin.plugin_runtime_handler,
        )

        kb = await store.get_kb_config()
        if not kb:
            logger.info(
                "[LongTermMemory] memory injection skipped: query_id=%s reason=no_kb_config",
                event_ctx.query_id,
            )
            return

        kb_id, config = kb
        pipeline_kbs = await api.list_pipeline_knowledge_bases()
        if not any(kb_entry.get("uuid") == kb_id for kb_entry in pipeline_kbs):
            logger.info(
                "[LongTermMemory] memory injection skipped: query_id=%s kb_id=%s reason=kb_not_in_pipeline",
                event_ctx.query_id,
                kb_id,
            )
            return

        # LangBot 4.11 uses this query scope for authorization, not only naive
        # RAG. Removing the memory KB would revoke access for our own retrieval
        # and tools. Preserve it until the Host exposes separate RAG suppression.
        query_vars = await api.get_query_vars()

        # --- L2 episodic memory retrieval ---
        retrieved_episodes: list[dict] = []
        user_message_text: str = query_vars.get("user_message_text", "")
        if user_message_text:
            try:
                auto_recall_top_k = self._resolve_auto_recall_top_k(config)
                entries = await api.retrieve_knowledge(
                    kb_id=kb_id,
                    query_text=user_message_text,
                    top_k=auto_recall_top_k,
                )
                if entries:
                    texts: list[str] = []
                    for i, entry in enumerate(entries, 1):
                        for content in entry.get("content", []):
                            if content.get("type") == "text" and content.get("text"):
                                texts.append(f"[{i}] {content['text']}")
                    retrieved_episodes = [
                        {"content": c["text"]}
                        for entry in entries
                        for c in entry.get("content", [])
                        if c.get("type") == "text" and c.get("text")
                    ]
                    if texts:
                        from qunling.memory_relevance import MEMORY_MARKER
                        l2_block = MEMORY_MARKER + "\n# Relevant Memories\n" + "\n\n".join(texts)
                        event_ctx.event.prompt.append(
                            Message(role="user", content=l2_block)
                        )
                        logger.info(
                            "[LongTermMemory] L2 episodic memory injected: query_id=%s kb_id=%s entry_count=%s",
                            event_ctx.query_id,
                            kb_id,
                            len(texts),
                        )
                from qunling.memory_recall import MARKER, completed
                await api.set_query_var(MARKER, completed(event_ctx.query_id,
                    getattr(event_ctx, "query_uuid", None), kb_id, user_message_text))
            except Exception:
                logger.exception(
                    "[LongTermMemory] L2 episodic retrieval failed: query_id=%s kb_id=%s",
                    event_ctx.query_id,
                    kb_id,
                )

        isolation = config.get("isolation", "session")
        bot_uuid = await api.get_bot_uuid()

        launcher_type, launcher_id = store.split_session_name(session_name)
        scope_key = store.get_session_key(bot_uuid, launcher_type, launcher_id)
        user_key = store.get_user_key(scope_key, isolation, bot_uuid)
        sender_id = str(query_vars.get("sender_id", "") or "")
        sender_name = str(query_vars.get("sender_name", "") or "")

        session_profile = await store.load_session_profile(scope_key)
        session_profile_block = store.format_profile_prompt(
            session_profile, "## Session Memory"
        )
        speaker_profile = None
        speaker_profile_block = ""
        if sender_id:
            speaker_profile = await store.load_speaker_profile(scope_key, sender_id)
            speaker_profile_block = store.format_profile_prompt(
                speaker_profile, "## Current Speaker Profile"
            )

        def _profile_summary(profile: dict | None) -> dict | None:
            if not profile:
                return None
            return {
                "name": profile.get("name", ""),
                "traits": profile.get("traits", []),
                "preferences": profile.get("preferences", []),
                "notes": profile.get("notes", ""),
                "updated_at": profile.get("updated_at", ""),
            }

        # --- context sharing for other plugins ---
        await api.set_query_var("_ltm_context", {
            "speaker": {"id": sender_id, "name": sender_name},
            "session_profile": _profile_summary(session_profile) or {
                "name": "",
                "traits": [],
                "preferences": [],
                "notes": "",
                "updated_at": "",
            },
            "speaker_profile": _profile_summary(speaker_profile),
            "episodes": retrieved_episodes,
        })

        # Build injection parts
        blocks: list[str] = []
        if session_profile_block.strip():
            blocks.append(session_profile_block)
        if speaker_profile_block.strip():
            blocks.append(speaker_profile_block)

        # Inject current speaker identity so LLM knows who is talking
        if sender_name:
            blocks.append(f"## Current Speaker\n- Name: {sender_name}\n- ID: {sender_id}")
        elif sender_id:
            blocks.append(f"## Current Speaker\n- ID: {sender_id}")

        async def _record_snapshot(injected: bool, injection_chars: int) -> None:
            """Persist the latest injection snapshot so the memory console can
            show what was actually recalled this turn (not only via logs)."""
            try:
                await store.save_injection_snapshot(scope_key, {
                    "recorded_at": store._now_timestamp(),
                    "query_id": event_ctx.query_id,
                    "session_name": session_name,
                    "scope_key": scope_key,
                    "user_key": user_key,
                    "kb_id": kb_id,
                    "isolation": isolation,
                    "speaker": {"id": sender_id, "name": sender_name},
                    "naive_rag_suppressed": False,
                    "session_profile": _profile_summary(session_profile),
                    "speaker_profile": _profile_summary(speaker_profile),
                    "episodes": retrieved_episodes,
                    "episode_count": len(retrieved_episodes),
                    "block_count": len(blocks),
                    "injected": injected,
                    "injection_chars": injection_chars,
                })
            except Exception:
                logger.exception(
                    "[LongTermMemory] failed to persist injection snapshot: query_id=%s scope_key=%s",
                    event_ctx.query_id,
                    scope_key,
                )

        if not blocks:
            await _record_snapshot(injected=False, injection_chars=0)
            logger.info(
                "[LongTermMemory] memory injection skipped: query_id=%s scope_key=%s sender_id=%s reason=no_profile_blocks",
                event_ctx.query_id,
                scope_key,
                sender_id,
            )
            return

        from qunling.memory_relevance import MEMORY_MARKER, MEMORY_POLICY
        injection = MEMORY_MARKER + "\n# Long-term Memory\n" + "\n\n".join(blocks)
        await _record_snapshot(injected=True, injection_chars=len(injection))
        logger.info(
            "[LongTermMemory] memory injection ready: query_id=%s kb_id=%s scope_key=%s sender_id=%s block_count=%s prompt_chars=%s",
            event_ctx.query_id,
            kb_id,
            scope_key,
            sender_id,
            len(blocks),
            len(injection),
        )

        event_ctx.event.default_prompt.append(
            Message(role="system", content=MEMORY_POLICY)
        )
        event_ctx.event.default_prompt.append(
            Message(role="user", content=injection)
        )

    @staticmethod
    def _candidate_config(config: dict) -> dict:
        def as_int(key: str, default: int) -> int:
            try:
                return int(config.get(key, default))
            except (TypeError, ValueError):
                return default

        return {
            "enabled": bool(config.get("candidate_extraction_enabled", False)),
            "auto_apply": bool(config.get("candidate_auto_apply", False)),
            "max_per_turn": max(1, min(10, as_int("candidate_max_per_turn", 3))),
        }

    @staticmethod
    def _candidate_text_from_query_vars(query_vars: dict, fallback: str = "") -> str:
        text = str(query_vars.get("user_message_text", "") or "").strip()
        return text or str(fallback or "").strip()

    @staticmethod
    def _redact_sensitive_candidate_text(text: str) -> str:
        replacements = (
            (r"(?i)(password\s*[:=]\s*)\S+", r"\1[REDACTED]"),
            (r"(?i)(api\s*key\s*[:=]\s*)\S+", r"\1[REDACTED]"),
            (r"(?i)(token\s*[:=]\s*)\S+", r"\1[REDACTED]"),
            (r"(?i)(secret\s*[:=]\s*)\S+", r"\1[REDACTED]"),
            (r"(验证码\s*[:：]?\s*)\S+", r"\1[REDACTED]"),
            (r"(密码\s*[:：]?\s*)\S+", r"\1[REDACTED]"),
            (r"(密钥\s*[:：]?\s*)\S+", r"\1[REDACTED]"),
            (r"(令牌\s*[:：]?\s*)\S+", r"\1[REDACTED]"),
        )
        redacted = text
        for pattern, replacement in replacements:
            redacted = re.sub(pattern, replacement, redacted)
        if redacted == text:
            return "[REDACTED sensitive content]"
        return redacted

    @staticmethod
    def _infer_candidates_from_text(
        text: str,
        *,
        sender_id: str = "",
        sender_name: str = "",
        max_candidates: int = 3,
    ) -> list[dict]:
        normalized = " ".join(text.split())
        if not normalized:
            return []

        speaker = sender_name or sender_id or "Current speaker"
        lowered = normalized.lower()
        candidates: list[dict] = []

        stable_markers = (
            "prefer",
            "prefers",
            "喜欢",
            "偏好",
            "叫我",
            "call me",
            "my name is",
        )
        temporal_markers = (
            "tomorrow",
            "next ",
            "deadline",
            "meeting",
            "plan",
            "travel",
            "flight",
            "今天",
            "明天",
            "下周",
            "计划",
            "截止",
            "会议",
            "航班",
        )
        sensitive_markers = (
            "password",
            "api key",
            "token",
            "secret",
            "验证码",
            "密码",
            "密钥",
            "令牌",
        )

        if any(marker in lowered for marker in sensitive_markers):
            candidates.append({
                "candidate_type": "ignore",
                "payload": {
                    "content": MemoryInjector._redact_sensitive_candidate_text(normalized),
                    "redacted": True,
                },
                "reason": "Potential secret or credential; do not store automatically.",
            })
        elif any(marker in lowered for marker in stable_markers):
            candidates.append({
                "candidate_type": "l1_profile",
                "payload": {
                    "target_scope": "speaker" if sender_id else "session",
                    "field": "preferences",
                    "action": "add",
                    "value": f"{speaker}: {normalized}",
                },
                "reason": "Stable preference or identity-like statement.",
            })
        elif any(marker in lowered for marker in temporal_markers):
            candidates.append({
                "candidate_type": "l2_episode",
                "payload": {
                    "content": f"{speaker}: {normalized}",
                    "tags": ["candidate"],
                    "importance": 2,
                },
                "reason": "Temporal plan, event, or situational fact.",
            })
        elif re.search(r"\b(i|we)\s+(decided|agreed|will|am|are)\b", lowered):
            candidates.append({
                "candidate_type": "l2_episode",
                "payload": {
                    "content": f"{speaker}: {normalized}",
                    "tags": ["candidate"],
                    "importance": 2,
                },
                "reason": "Potentially useful decision or status update.",
            })

        return candidates[:max_candidates]

    async def _extract_memory_candidates(self, event_ctx: context.EventContext) -> None:
        store = self.plugin.memory_store
        event = event_ctx.event
        api = QueryBasedAPIProxy(
            query_id=event_ctx.query_id,
            plugin_runtime_handler=self.plugin.plugin_runtime_handler,
        )

        kb = await store.get_kb_config()
        if not kb:
            return
        kb_id, config = kb
        candidate_config = self._candidate_config(config)
        if not candidate_config["enabled"]:
            return

        bot_uuid = await api.get_bot_uuid()
        query_vars = await api.get_query_vars()
        session = getattr(event, "session", None)
        if session is None:
            return
        session_key, user_key, _kb_id, _isolation, _config = await store.resolve_user_context(
            session,
            bot_uuid,
        )
        sender_id = str(getattr(event, "sender_id", "") or query_vars.get("sender_id", "") or "")
        sender_name = str(query_vars.get("sender_name", "") or "")
        text = self._candidate_text_from_query_vars(
            query_vars,
            getattr(event, "response_text", ""),
        )
        candidates = self._infer_candidates_from_text(
            text,
            sender_id=sender_id,
            sender_name=sender_name,
            max_candidates=candidate_config["max_per_turn"],
        )
        if not candidates:
            return

        embedding_model_uuid = str(config.get("embedding_model_uuid", "") or "")
        for candidate in candidates:
            entry = await store.append_memory_candidate(
                scope_key=session_key,
                user_key=user_key,
                candidate_type=candidate["candidate_type"],
                payload=candidate["payload"],
                reason=candidate["reason"],
                sender_id=sender_id,
                sender_name=sender_name,
                query_id=event_ctx.query_id,
            )
            if not candidate_config["auto_apply"]:
                continue
            if not embedding_model_uuid:
                continue
            accepted = await store.accept_memory_candidate(
                session_key,
                entry["candidate_id"],
                collection_id=kb_id,
                embedding_model_uuid=embedding_model_uuid,
                user_key=user_key,
                bot_uuid=bot_uuid,
            )
            if accepted:
                await store.append_audit_entry(
                    scope_key=session_key,
                    user_key=user_key,
                    operation="candidate_auto_apply",
                    target_type="candidate",
                    target_id=entry["candidate_id"],
                    summary=f"Auto-applied memory candidate {entry['candidate_id']}",
                    sender_id=sender_id,
                    sender_name=sender_name,
                    query_id=event_ctx.query_id,
                    metadata={"candidate": accepted},
                )
