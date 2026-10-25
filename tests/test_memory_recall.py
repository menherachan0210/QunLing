from copy import deepcopy
from types import SimpleNamespace

from qunling.memory_recall import MARKER, RUNNER, completed, deduplicate


def test_only_completed_same_query_recall_changes_automatic_selector():
    query = SimpleNamespace(query_id=1, query_uuid="this-query", variables={"user_message_text": "remember this"})
    config = {"knowledge-bases": ["memory", "docs"]}
    resources = {"knowledge_bases": [{"kb_id": "memory"}, {"kb_id": "docs"}]}
    context = {"config": config, "resources": deepcopy(resources)}
    assert not deduplicate(query, context, RUNNER)
    query.variables[MARKER] = completed(1, "this-query", "memory", "remember this")
    assert not deduplicate(query, context, "different-runner")
    assert deduplicate(query, context, RUNNER)
    assert context["config"]["knowledge-bases"] == ["docs"]
    assert config["knowledge-bases"] == ["memory", "docs"]
    assert context["resources"] == resources
    query.query_uuid = "other-query"
    assert not deduplicate(query, {"config": config}, RUNNER)
    query.query_uuid = "this-query"
    query.variables["user_message_text"] = "new text"
    assert not deduplicate(query, {"config": config}, RUNNER)
