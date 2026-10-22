"""Upgrade only the complete known dispatch patch, never an arbitrary line."""
import pytest
from test_runtime_patches import ROOT, patcher, proactive_originals

NAME = "host-qq-dispatch.patch"
PATH = "src/langbot/pkg/provider/tools/toolmgr.py"
OLD = "            result = await invoke_tool(self.ap, query, invoke, name)\n"
NEW = "            result = await invoke_tool(self.ap, query, invoke, name, source=source)\n"


def installed_legacy(tmp_path):
    source = (ROOT / "patches" / NAME).read_text(encoding="utf-8")
    originals = proactive_originals(tmp_path, source)
    legacy, _ = patcher.runtime_payload(source.replace("+" + NEW, "+" + OLD), "host")
    assert patcher.git_apply(tmp_path, legacy).returncode == 0
    return source, originals


def test_old_dispatch_installs_source_guard_idempotently_without_other_changes(tmp_path):
    source, originals = installed_legacy(tmp_path)
    before = {p: (tmp_path / p).read_bytes() for p in originals}
    plan = patcher.build_plan(NAME, tmp_path, source, "host")
    assert plan.upgrade_from and plan.verify_payload
    patcher.execute_plans([plan], apply=False)
    assert before == {p: (tmp_path / p).read_bytes() for p in originals}
    patcher.execute_plans([plan], apply=True)
    assert NEW in (tmp_path / PATH).read_text(encoding="utf-8")
    assert patcher.build_plan(NAME, tmp_path, source, "host").state == "already-applied"
    assert all((tmp_path / p).read_bytes() == content for p, content in before.items() if p != PATH)


@pytest.mark.parametrize("after_plan", [False, True])
def test_incomplete_or_concurrently_changed_dispatch_fails_closed(tmp_path, after_plan):
    source, _ = installed_legacy(tmp_path)
    if after_plan:
        plan = patcher.build_plan(NAME, tmp_path, source, "host")
    target = tmp_path / "src/langbot/pkg/pipeline/aggregator.py"
    target.write_text(target.read_text(encoding="utf-8").replace("forward_ingress", "changed_ingress"), encoding="utf-8")
    with pytest.raises(patcher.PatchError):
        if after_plan:
            patcher.execute_plans([plan], apply=True)
        else:
            patcher.build_plan(NAME, tmp_path, source, "host")
    assert OLD in (tmp_path / PATH).read_text(encoding="utf-8")
