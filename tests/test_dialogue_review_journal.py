"""Verify the installed Host hook against its real SQLite transcript writer."""
import importlib.util
from pathlib import Path

import pytest
import pytest_asyncio

from qunling import dialogue_review

HOST = Path(next(iter(importlib.util.find_spec("langbot").submodule_search_locations))).parents[1]
SOURCE = HOST / "tests/unit_tests/agent/test_orchestrator_integration.py"
if not SOURCE.is_file():
    pytest.skip("Requires pinned Host integration-test helpers", allow_module_level=True)
SPEC = importlib.util.spec_from_file_location("host_review_test_helpers", SOURCE)
helpers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helpers)
clean_agent_state = pytest_asyncio.fixture(helpers.clean_agent_state.__wrapped__)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["message.completed", "run.completed"])
async def test_review_precedes_journal_and_delivery(clean_agent_state, monkeypatch, kind):
    from langbot.pkg.agent.runner.transcript_store import TranscriptStore

    seen = []
    class Reviewer:
        def __init__(self, query):
            pass

        async def observe(self, result):
            seen.append(result)
            return {**result, "data": {**result["data"], "message": {"role": "assistant", "content": "实际发送的文本"}}}

    monkeypatch.setattr(dialogue_review, "RunReview", Reviewer)
    connector = helpers.FakePluginConnector(results=[{"type": kind,
        "data": {"message": {"role": "assistant", "content": "被改掉的草稿"}}}])
    app = helpers.FakeApplication(connector, clean_agent_state)
    orchestrator = helpers.AgentRunOrchestrator(app, helpers.FakeRegistry(helpers.make_descriptor()))
    query = helpers.make_query()
    object.__setattr__(query, dialogue_review.ATTRIBUTE, object())
    replies = [item async for item in orchestrator.run_from_query(query)]
    assert len(seen) == 1
    assert [item.content for item in replies] == ["实际发送的文本"]
    items, _, _, _ = await TranscriptStore(clean_agent_state).page_transcript(query.session.using_conversation.uuid, limit=10)
    assert [item["content"] for item in items if item["role"] == "assistant"] == ["实际发送的文本"]
