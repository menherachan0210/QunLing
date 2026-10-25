"""Offline tests for shared group recall; no plugin writes or model calls."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.sdk

from qunling import group_memory as memory


def entry(text="喜欢校对漫画", *, group="30000", bot="bot", speaker="20000", **metadata):
    return NS(metadata={
        "user_key": f"{bot}:group_{group}", "bot_uuid": bot,
        "content": text, "sender_id": speaker, "sender_name": f"群友{speaker}",
        "timestamp": "2026-10-21T01:00:00Z", "status": "active", **metadata,
    }, content=[{"type": "file", "file_id": "internal-file-handle"}])


@pytest.fixture
def source():
    context = NS(instance_uuid="instance", workspace_uuid="workspace", placement_generation=1, bot_uuid="bot")
    bot = NS(bot_entity=NS(uuid="bot", workspace_uuid="workspace", enable=True),
             execution_context=context, enable=True, assert_execution_active=AsyncMock(),
             get_pipeline_target_for_event_type=lambda event: "pipeline")
    pipeline = NS(bound_plugins=None, pipeline_entity=NS(config={"ai": {
        "runner": {"id": "runner"}, "runner_config": {"runner": {"memory-selector": ["memory"]}},
    }}))
    descriptor = NS(config_schema=[{"name": "memory-selector", "type": "knowledge-base-multi-selector"}],
                    supports_knowledge_retrieval=lambda: True)
    kb = NS(get_knowledge_engine_plugin_id=lambda: memory.MEMORY_PLUGIN,
            knowledge_base_entity=NS(workspace_uuid="workspace", creation_settings={
                "embedding_model_uuid": "embedding", "isolation": "session", "auto_recall_top_k": 3,
            }), retrieve=AsyncMock(return_value=[entry()]))
    bot.ap = NS(workspace_service=NS(instance_uuid="instance"),
                bot_service=NS(get_bot=AsyncMock(return_value={"enable": True, "adapter": "onebot-v11"})),
                platform_mgr=NS(get_bot_by_uuid=AsyncMock(return_value=bot)),
                pipeline_mgr=NS(get_pipeline_by_uuid=AsyncMock(return_value=pipeline)),
                runner_registry=NS(get=AsyncMock(return_value=descriptor)),
                rag_mgr=NS(get_knowledge_base_by_uuid=AsyncMock(return_value=kb)))
    return NS(bot=bot, context=context, pipeline=pipeline, kb=kb, descriptor=descriptor)


async def recall(source, text="你还记得我的爱好吗", *, participant="20000"):
    return await memory.recall_group_memory(source.bot, "30000", participant, text)


@pytest.mark.asyncio
async def test_uses_existing_pipeline_memory_with_real_group_and_member_and_no_writes(source):
    original = deepcopy(source.pipeline.pipeline_entity.config)
    settings = deepcopy(source.kb.knowledge_base_entity.creation_settings)
    result = await recall(source)
    assert result == {"group_id": "30000", "participant_id": "20000", "records": [{
        "kind": "episode",
        "speaker_id": "20000", "speaker_name": "群友20000", "text": "喜欢校对漫画",
        "timestamp": "2026-10-21T01:00:00Z",
    }]}
    source.bot.ap.runner_registry.get.assert_awaited_once_with(source.context, "runner", None)
    source.bot.ap.rag_mgr.get_knowledge_base_by_uuid.assert_awaited_once_with(source.context, "memory")
    source.kb.retrieve.assert_awaited_once_with(source.context, "你还记得我的爱好吗", settings={
        "session_name": "group_30000", "bot_uuid": "bot", "sender_id": "20000", "top_k": 3, "include_profiles": True,
    })
    assert source.pipeline.pipeline_entity.config == original
    assert source.kb.knowledge_base_entity.creation_settings == settings


@pytest.mark.asyncio
async def test_shared_group_records_keep_each_speaker_and_omit_untrusted_metadata(source):
    source.kb.retrieve.return_value = [
        entry("A 提议周六讨论", speaker="20001", api_key="never-forward-key", file_ref="internal-ref"),
        entry("B 要负责校对", speaker="20002"),
    ]
    result = await recall(source)
    assert [r["speaker_id"] for r in result["records"]] == ["20001", "20002"]
    assert result["participant_id"] == "20000"
    serialized = json.dumps(result, ensure_ascii=False)
    assert "never-forward-key" not in serialized and "internal-" not in serialized
    assert "user_key" not in serialized and "api_key" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [
    {"user_key": "bot:group_90000"}, {"user_key": "bot:person_20000"},
    {"user_key": "other-bot:group_30000"}, {"user_key": "bot:bot"},
    {"user_key": "group_30000"}, {"user_key": None}, {"bot_uuid": "other-bot"},
    {"status": "deleted"}, {"status": "superseded"}, {"superseded_by": "new-record"},
])
async def test_rejects_foreign_unscoped_or_inactive_records_even_if_plugin_returns_them(source, metadata):
    source.kb.retrieve.return_value = [entry("must not escape", **metadata)]
    assert (await recall(source))["records"] == []


@pytest.mark.asyncio
async def test_unknown_speaker_is_not_assigned_to_current_participant(source):
    source.kb.retrieve.return_value = [entry("某位成员曾提出建议", sender_id="", sender_name="")]
    result = await recall(source)
    assert result["records"][0]["speaker_id"] == ""
    assert result["records"][0]["speaker_name"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["global", "foreign_workspace", "ordinary_knowledge"])
async def test_only_group_isolated_long_term_memory_is_retrieved(source, kind):
    if kind == "global":
        source.kb.knowledge_base_entity.creation_settings["isolation"] = "global"
    elif kind == "foreign_workspace":
        source.kb.knowledge_base_entity.workspace_uuid = "other"
    else:
        source.kb.get_knowledge_engine_plugin_id = lambda: "langbot-team/LangRAG"
    assert (await recall(source))["records"] == []
    source.kb.retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_embedding_still_allows_existing_profiles(source):
    source.kb.knowledge_base_entity.creation_settings.pop("embedding_model_uuid")
    source.kb.retrieve.return_value = [entry("当前成员喜欢校对", memory_kind="speaker_profile")]
    result = await recall(source)
    assert result["records"][0]["kind"] == "speaker_profile"
    source.kb.retrieve.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["disabled", "persisted_disabled", "runtime_replaced", "wrong_bot", "wrong_workspace", "wrong_instance"])
async def test_runtime_authorization_failure_never_calls_retriever(source, kind):
    if kind == "disabled":
        source.bot.enable = False
    elif kind == "persisted_disabled":
        source.bot.ap.bot_service.get_bot.return_value = {"enable": False, "adapter": "onebot-v11"}
    elif kind == "runtime_replaced":
        source.bot.ap.platform_mgr.get_bot_by_uuid.return_value = NS()
    elif kind == "wrong_bot":
        source.context.bot_uuid = "another-bot"
    elif kind == "wrong_workspace":
        source.context.workspace_uuid = "another-workspace"
    else:
        source.context.instance_uuid = "another-instance"
    assert (await recall(source))["records"] == []
    source.kb.retrieve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["plugin_disabled", "kb_not_selected", "schema_not_kb", "runner_no_retrieval"])
async def test_pipeline_cannot_recall_unselected_or_unauthorized_memory(source, kind):
    if kind == "plugin_disabled":
        source.pipeline.bound_plugins = ["other/Plugin"]
    elif kind == "kb_not_selected":
        source.pipeline.pipeline_entity.config["ai"]["runner_config"]["runner"]["memory-selector"] = []
    elif kind == "schema_not_kb":
        source.descriptor.config_schema[0]["type"] = "input"
    else:
        source.descriptor.supports_knowledge_retrieval = lambda: False
    assert (await recall(source))["records"] == []
    source.kb.retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoking_pipeline_while_recalling_discards_results(source):
    async def retrieve(*args, **kwargs):
        source.pipeline.pipeline_entity.config["ai"]["runner_config"]["runner"]["memory-selector"] = []
        return [entry("no longer authorized")]
    source.kb.retrieve.side_effect = retrieve
    assert (await recall(source))["records"] == []


@pytest.mark.asyncio
async def test_placement_change_during_recall_discards_results(source):
    async def retrieve(*args, **kwargs):
        source.context.placement_generation += 1
        return [entry("stale placement")]
    source.kb.retrieve.side_effect = retrieve
    assert (await recall(source))["records"] == []


@pytest.mark.asyncio
async def test_timeout_cancels_retrieval_and_returns_empty_without_exposing_exception(source, monkeypatch, caplog):
    monkeypatch.setattr(memory, "RECALL_TIMEOUT", 0.02)
    cancelled = asyncio.Event()
    async def retrieve(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    source.kb.retrieve.side_effect = retrieve
    assert (await recall(source))["records"] == []
    assert cancelled.is_set()
    source.kb.retrieve.side_effect = RuntimeError("api_key=private-key-and-endpoint")
    with caplog.at_level("INFO"):
        assert (await recall(source))["records"] == []
    assert "private-key-and-endpoint" not in caplog.text


@pytest.mark.asyncio
async def test_external_cancellation_propagates_and_leaves_no_retrieval_task(source):
    started, stopped = asyncio.Event(), asyncio.Event()
    async def retrieve(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    source.kb.retrieve.side_effect = retrieve
    task = asyncio.create_task(recall(source))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_bounds_query_and_records_and_sanitizes_internal_content(source):
    source.kb.retrieve.return_value = [entry(
        "api_key=private-key file://private/asset file_ref=internal-id webfile_secretfilehandle asset_" + "a" * 32 + " 群聊记录" * 1000,
        speaker=str(20000 + index), sender_name="Bearer private-token", timestamp="not-a-date",
    ) for index in range(20)]
    result = await recall(source, "可用线索" * 1000)
    assert len(source.kb.retrieve.call_args.args[1]) == memory.MAX_QUERY_CHARS
    assert len(result["records"]) == memory.MAX_RECORDS
    assert all(len(r["text"]) <= memory.MAX_RECORD_CHARS and r["timestamp"] == "" for r in result["records"])
    serialized = json.dumps(result)
    assert "private-key" not in serialized and "internal-id" not in serialized and "private-token" not in serialized
    assert "secretfilehandle" not in serialized and "asset_" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("group,participant,text", [("person_1", "20000", "hi"), ("30000", "", "hi"),
    (True, "20000", "hi"), ("30000", "20000", ""), ("30000", "20000", {"query": "hi"})])
async def test_invalid_or_empty_ingress_does_no_recall(source, group, participant, text):
    assert (await memory.recall_group_memory(source.bot, group, participant, text))["records"] == []
    source.bot.assert_execution_active.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_members_do_not_share_actor_or_result_cache(source):
    async def retrieve(context, text, *, settings):
        await asyncio.sleep(0)
        return [entry(text, speaker=settings["sender_id"])]
    source.kb.retrieve.side_effect = retrieve
    first, second = await asyncio.gather(recall(source, "A 的提问", participant="20001"),
                                         recall(source, "B 的提问", participant="20002"))
    assert first["records"][0]["speaker_id"] == first["participant_id"] == "20001"
    assert second["records"][0]["speaker_id"] == second["participant_id"] == "20002"
    assert first["records"][0]["text"] == "A 的提问"
    assert second["records"][0]["text"] == "B 的提问"
