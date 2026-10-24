"""Real QQ admission with private continuation grants; no model or QQ I/O."""
import asyncio
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from test_qq_sessions import app
from test_qq_dispatch import managed, put, until
from langbot_plugin.api.entities.builtin.platform import message as pm, entities as pe, events
from langbot_plugin.api.entities.builtin.provider.session import LauncherTypes
from langbot.pkg.pipeline.controller import Controller
from langbot.pkg.pipeline.entities import ResultType
from langbot.pkg.provider.tools import toolmgr
from qunling import qq_dispatch as dispatch
from qunling.proactive import host


def arguments(app, *, sender="20000", group="30000", text="那这张图到底像不像", variables=None):
    chain = pm.MessageChain([pm.Plain(text=text)])
    event = events.GroupMessage(type="GroupMessage", sender=pe.GroupMember(
        id=sender, member_name=sender, permission=pe.Permission.Member,
        group=pe.Group(id=group, name="Synthetic", permission=pe.Permission.Member)),
        message_chain=chain, time=1609459200)
    return dict(bot_uuid="bot", launcher_type=LauncherTypes.GROUP, launcher_id=group,
                sender_id=sender, message_event=event, message_chain=chain,
                adapter=app.adapter, pipeline_uuid="pipeline", execution_context=app.scope,
                routed_by_rule=False, variables=variables)


async def observed(app, **kwargs):
    values = arguments(app, **kwargs)
    assert await app.query_pool.add_query(**values) is None
    record = app.dispatch.continuations[id(values["message_event"])]
    await asyncio.wait_for(record.completion.wait(), 1)
    assert record.observed
    return values


async def continuation(app, *, allow_actions=False, **kwargs):
    values = await observed(app, **kwargs)
    query = await dispatch.admit_continuation(app, values, validate=lambda: True, allow_actions=allow_actions)
    assert query is not None
    return query


@pytest.mark.asyncio
async def test_natural_task_gets_same_tool_path_without_fabricating_at_or_losing_owner(managed):
    query = await continuation(managed, allow_actions=True, text="把刚才那张改成水彩，做好发出来")
    assert query.sender_id == "20000" and not any(isinstance(p, pm.At) for p in query.message_chain)
    controller = Controller(managed)
    assert await dispatch.acquire_slot(controller, query)
    invoke = AsyncMock(return_value="completed")
    for name in ("generate_image", "edit_image", "send_file", "file_workspace"):
        assert await dispatch.invoke_tool(managed, query, invoke, name, source="plugin",
            parameters={"operation": "create", "deliver": True}) == "completed"
    query._qq_continuation.validate = lambda: False
    assert json.loads(await dispatch.invoke_tool(managed, query, invoke, "generate_image", source="plugin"))["executed"] is False
    assert invoke.await_count == 4


@pytest.mark.asyncio
async def test_query_variables_cannot_upgrade_a_research_grant_to_actions(managed):
    query = await continuation(managed, variables={"allow_actions": True, "natural_task_request": True})
    assert await dispatch.acquire_slot(Controller(managed), query)
    invoke = AsyncMock()
    result = await dispatch.invoke_tool(managed, query, invoke, "send_file", source="plugin")
    assert json.loads(result)["executed"] is False
    invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_original_message_reenters_once_without_fake_mention_or_rewriting_identity(managed):
    values = await observed(managed)
    original = values["message_event"].model_dump()
    validate = AsyncMock(return_value=True)
    query = await dispatch.admit_continuation(managed, values, validate=validate)
    assert query in managed.query_pool.queries
    assert query.message_event is values["message_event"]
    assert query.sender_id == "20000" and query.launcher_id == "30000"
    assert values["message_event"].model_dump() == original
    assert not any(isinstance(part, pm.At) for part in query.message_chain)
    assert query._qq_continuation is query._qq_task.continuation
    assert validate.await_count >= 2
    # The real trigger stage consumes only the host replacement, not variables.
    managed.trigger.rule_matchers = []
    query.pipeline_config = managed.pipeline.pipeline_entity.config
    assert (await managed.trigger.process(query, "trigger")).result_type == ResultType.CONTINUE
    assert str(query.message_chain) == str(values["message_chain"])
    assert await dispatch.admit_continuation(managed, values, validate=validate) is None
    assert len(managed.query_pool.queries) == 1
    assert managed.plugin_connector.emit_event.await_count == 1


@pytest.mark.asyncio
async def test_unobserved_or_client_variable_capabilities_never_admit(managed):
    values = arguments(managed, variables={"_qq_continuation": True, "qq_admission_bypass": True})
    validate = AsyncMock(return_value=True)
    assert await dispatch.admit_continuation(managed, values, validate=validate) is None
    validate.assert_not_awaited()
    assert await managed.query_pool.add_query(**values) is None
    assert not managed.query_pool.queries
    assert dispatch._continuation.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["sender", "group", "bot", "pipeline", "scope", "event", "content"])
async def test_copied_mutated_or_cross_owner_arguments_cannot_take_over_original(managed, change):
    values = await observed(managed)
    candidate = dict(values)
    if change == "sender":
        candidate["sender_id"] = "someone-else"
    elif change == "group":
        candidate["launcher_id"] = "40000"
    elif change == "bot":
        candidate["bot_uuid"] = "other-bot"
    elif change == "pipeline":
        candidate["pipeline_uuid"] = "other-pipeline"
    elif change == "scope":
        candidate["execution_context"] = NS(instance_uuid="instance", workspace_uuid="other", placement_generation=1)
    elif change == "event":
        candidate["message_event"] = values["message_event"].model_copy(deep=True)
    else:
        values["message_chain"].append(pm.Plain(text="偷偷追加新任务"))
    validate = Mock(return_value=True)
    assert await dispatch.admit_continuation(managed, candidate, validate=validate) is None
    assert not managed.query_pool.queries
    validate.assert_not_called()


@pytest.mark.asyncio
async def test_remember_hook_runs_after_unmatched_completion_and_never_for_matched_trigger(managed, monkeypatch):
    order = []
    monkeypatch.setattr(host, "completed", lambda *args: order.append("complete"))
    monkeypatch.setattr(host, "remember_ingress", lambda *args: order.append("remember"))
    values = await observed(managed)
    assert order == ["complete", "remember"]
    await put(managed, "30001", "explicit request")
    assert order == ["complete", "remember"]
    query = await dispatch.admit_continuation(managed, values, validate=lambda: True)
    assert query is not None and order == ["complete", "remember"]


@pytest.mark.asyncio
async def test_final_authorization_is_checked_after_async_pipeline_lookup(managed):
    values = await observed(managed)
    validate = AsyncMock(side_effect=[True, False])
    assert await dispatch.admit_continuation(managed, values, validate=validate) is None
    assert validate.await_count == 2
    assert not managed.query_pool.queries and not managed.dispatch.preparing
    assert dispatch._continuation.get() is None
    managed.adapter.reply_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_successful_original_observation_can_be_readmitted(managed):
    values = arguments(managed)
    entered, release = asyncio.Event(), asyncio.Event()
    async def observe(*args):
        entered.set()
        await release.wait()
        raise RuntimeError("synthetic observation failure")
    managed.plugin_connector.emit_event.side_effect = observe
    assert await managed.query_pool.add_query(**values) is None
    await entered.wait()
    pending = asyncio.create_task(dispatch.admit_continuation(managed, values, validate=lambda: True))
    await asyncio.sleep(0)
    assert not pending.done() and not managed.query_pool.queries
    release.set()
    assert await pending is None
    assert not managed.query_pool.queries


@pytest.mark.asyncio
async def test_concurrent_attempts_claim_one_original_message_once(managed):
    values = await observed(managed)
    queries = await asyncio.gather(*(dispatch.admit_continuation(managed, values, validate=lambda: True) for _ in range(4)))
    assert sum(query is not None for query in queries) == 1
    assert len(managed.query_pool.queries) == 1


@pytest.mark.asyncio
async def test_inherited_child_context_cannot_spend_parent_capability(managed):
    values = await observed(managed)
    validations = 0
    async def validate():
        nonlocal validations
        validations += 1
        if validations == 2:
            # create_task inherits ContextVars, but not the admission task's
            # authority to spend this exact one-shot capability.
            assert await asyncio.create_task(managed.query_pool.add_query(**values)) is None
        return True
    query = await dispatch.admit_continuation(managed, values, validate=validate)
    assert query is not None and managed.query_pool.queries == [query]
    assert dispatch._continuation.get() is None


@pytest.mark.asyncio
async def test_cancelled_admission_clears_capability_and_preparing_state(managed):
    values = await observed(managed)
    entered = asyncio.Event()
    validations = 0
    async def validate():
        nonlocal validations
        validations += 1
        if validations == 2:
            entered.set()
            await asyncio.Event().wait()
        return True
    pending = asyncio.create_task(dispatch.admit_continuation(managed, values, validate=validate))
    await entered.wait()
    pending.cancel()
    result = await asyncio.gather(pending, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert not managed.query_pool.queries and not managed.dispatch.preparing
    assert dispatch._continuation.get() is None
    assert await dispatch.admit_continuation(managed, values, validate=lambda: True) is None


@pytest.mark.asyncio
async def test_continuation_keeps_bounded_pool_and_cannot_displace_explicit_request(managed):
    values = await observed(managed)
    managed.query_pool.max_queries = managed.query_pool.max_queries_per_workspace = 1
    original = await put(managed, "other", "explicit")
    assert await dispatch.admit_continuation(managed, values, validate=lambda: True) is None
    assert managed.query_pool.queries == [original]
    assert not original._qq_task.done.is_set()
    managed.adapter.reply_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_continuation_is_stopped_when_authorization_changes(managed):
    values = await observed(managed)
    allowed = [True]
    query = await dispatch.admit_continuation(managed, values, validate=lambda: allowed[0])
    managed.pipeline.run = AsyncMock()
    allowed[0] = False
    await dispatch.run_pipeline(managed, query, managed.pipeline)
    managed.pipeline.run.assert_not_awaited()
    assert query._qq_task.done.is_set() and query not in managed.query_pool.queries
    managed.adapter.reply_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_preflight_revocation_closes_unstarted_operation(managed):
    query = await continuation(managed)
    managed.adapter._runtime.enable = False
    operation = AsyncMock()
    with pytest.raises(RuntimeError, match="revoked"):
        await dispatch.preflight(query, operation())
    operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_continuation_denies_all_unreviewed_tools_and_non_plugin_sources(managed):
    query = await continuation(managed)
    invoke = AsyncMock(return_value="unsafe side effect")
    blocked = ("generate_image", "edit_image", "send_attachment", "send_web_images", "send_web_file",
               "download_web_file", "file_workspace", "file_task", "browse_group_files",
               "import_group_context_images", "remember", "forget", "update_profile",
               "start_comic_translation", "delete_comic_translation", "cancel_comic_translation",
               "resume_comic_translation", "send_comic_translation_result", "arbitrary_new_tool", "")
    for name in blocked:
        result = json.loads(await dispatch.invoke_tool(managed, query, invoke, name, source="plugin"))
        assert result["status"] == "blocked" and result["executed"] is False
    for source in ("mcp", "native", "skill", "", "plugin_tool"):
        result = json.loads(await dispatch.invoke_tool(managed, query, invoke, "search_web", source=source))
        assert result["status"] == "blocked"
    invoke.assert_not_awaited()
    assert not query._qq_task.done.is_set() and managed.dispatch.parked == 0
    # The trusted registry continues enforcing policy on a reconstructed view.
    view = query.model_copy()
    object.__delattr__(view, "_qq_continuation")
    object.__delattr__(view, "_qq_task")
    assert json.loads(await dispatch.invoke_tool(managed, view, invoke, "generate_image", source="plugin"))["executed"] is False


@pytest.mark.asyncio
async def test_reviewed_read_tools_keep_normal_tool_lease_and_owner(managed):
    query = await continuation(managed)
    controller = Controller(managed)
    assert await dispatch.acquire_slot(controller, query)
    async def read():
        assert query._qq_task.phase == "tool" and not query._qq_task.owns_slot
        return {"observation": "actual pixels"}
    for name in dispatch.CONTINUATION_READ_TOOLS:
        assert await dispatch.invoke_tool(managed, query, read, name, source="plugin") == {"observation": "actual pixels"}
        assert query._qq_task.owns_slot and managed.dispatch.parked == 0
    dispatch.release_slot(controller, query)
    assert controller.semaphore._value == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("name,parameters", [
    *[("file_workspace", {"operation": operation, "arguments": {}})
      for operation in ("list", "import", "inspect", "read", "search", "extract")],
    ("file_task", {"action": "list"}), ("file_task", {"action": "status", "task_id": "task"}),
    ("browse_group_files", {}), ("browse_group_files", {"action": "import", "file_ref": "file"}),
])
async def test_read_only_attachment_operations_use_the_existing_task_lease(managed, name, parameters):
    query = await continuation(managed)
    controller = Controller(managed)
    assert await dispatch.acquire_slot(controller, query)
    invoke = AsyncMock(return_value={"status": "pending", "task_id": "read-task"})
    assert await dispatch.invoke_tool(managed, query, invoke, name, source="plugin", parameters=parameters) == {
        "status": "pending", "task_id": "read-task"}
    invoke.assert_awaited_once()
    assert query._qq_task.owns_slot and managed.dispatch.parked == 0
    dispatch.release_slot(controller, query)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,parameters", [
    ("file_workspace", {"operation": "create"}), ("file_workspace", {"operation": "delete"}),
    ("file_workspace", {"operation": "extract", "deliver": True}),
    ("file_workspace", {"operation": "extract", "deliver": "false"}),
    ("file_workspace", {"operation": "read", "arguments": []}),
    ("file_task", {"action": "cancel", "task_id": "task"}),
    ("browse_group_files", {"action": "delete", "file_ref": "file"}),
])
async def test_read_only_attachment_grant_never_delivers_or_mutates_external_content(managed, name, parameters):
    query = await continuation(managed)
    invoke = AsyncMock()
    result = await dispatch.invoke_tool(managed, query, invoke, name, source="plugin", parameters=parameters)
    assert json.loads(result)["executed"] is False
    invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_attachment_operation_is_rechecked_after_waiting_for_capacity(managed):
    query = await continuation(managed)
    controller = Controller(managed)
    assert await dispatch.acquire_slot(controller, query)
    await managed.dispatch.tools.acquire()
    parameters = {"operation": "extract", "deliver": False}
    invoke = AsyncMock()
    pending = asyncio.create_task(dispatch.invoke_tool(managed, query, invoke, "file_workspace", source="plugin", parameters=parameters))
    await until(lambda: query._qq_task.phase == "tool_wait")
    parameters["deliver"] = True
    managed.dispatch.tools.release()
    assert json.loads(await pending)["executed"] is False
    invoke.assert_not_awaited()
    assert query._qq_task.owns_slot and managed.dispatch.parked == 0
    dispatch.release_slot(controller, query)


@pytest.mark.asyncio
async def test_real_tool_manager_passes_trusted_source_into_continuation_gate(managed, tmp_path):
    # Apply the actual checked-in runtime patch to an isolated source copy.
    # The developer's ignored LangBot checkout may still have the older patch.
    root = Path(__file__).resolve().parents[1]
    path = "src/langbot/pkg/provider/tools/toolmgr.py"
    source = Path(toolmgr.__file__).read_text(encoding="utf-8")
    for call in ("invoke_tool(self.ap, query, invoke, name)",
                 "invoke_tool(self.ap, query, invoke, name, source=source, parameters=parameters)",
                 "invoke_tool(self.ap, query, invoke, name, source=source)"):
        source = source.replace("            from qunling.qq_dispatch import invoke_tool\n\n"
                                + "            result = await " + call,
                                "            result = await invoke()")
    copy = tmp_path / path
    copy.parent.mkdir(parents=True)
    copy.write_text(source, encoding="utf-8", newline="\n")
    patch = (root / "patches/host-qq-dispatch.patch").read_text(encoding="utf-8")
    section = next("diff --git " + section for section in patch.split("diff --git ")
                   if section.startswith(f"a/{path} b/{path}\n"))
    applied = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], input=section.encode("utf-8"),
                             cwd=tmp_path, capture_output=True)
    assert applied.returncode == 0, applied.stderr.decode("utf-8", errors="replace")
    namespace = dict(vars(toolmgr))
    exec(compile(copy.read_text(encoding="utf-8"), str(copy), "exec"), namespace)
    patched_manager = namespace["ToolManager"]
    query = await continuation(managed)
    controller = Controller(managed)
    assert await dispatch.acquire_slot(controller, query)
    manager = object.__new__(patched_manager)
    manager.ap = managed
    manager._record_tool_call = AsyncMock()
    manager._trace_tool_call = Mock()
    invoke = AsyncMock(return_value="read result")
    assert await manager._invoke_tool_with_monitoring(source="plugin", name="read_qq_avatar",
        parameters={}, query=query, invoke=invoke) == "read result"
    invoke.assert_awaited_once()
    assert await manager._invoke_tool_with_monitoring(source="plugin", name="file_workspace",
        parameters={"operation": "read", "arguments": {"asset_ref": "trusted-file"}},
        query=query, invoke=invoke) == "read result"
    assert invoke.await_count == 2
    result = await manager._invoke_tool_with_monitoring(source="plugin", name="file_workspace",
        parameters={"operation": "extract", "deliver": True}, query=query, invoke=invoke)
    assert json.loads(result)["executed"] is False and invoke.await_count == 2
    result = await manager._invoke_tool_with_monitoring(source="mcp", name="read_qq_avatar",
        parameters={}, query=query, invoke=invoke)
    assert json.loads(result)["executed"] is False
    assert invoke.await_count == 2
    dispatch.release_slot(controller, query)


@pytest.mark.asyncio
async def test_revoke_while_tool_waits_prevents_invoke_and_restores_slots(managed):
    values = await observed(managed)
    allowed = [True]
    query = await dispatch.admit_continuation(managed, values, validate=lambda: allowed[0])
    controller = Controller(managed)
    assert await dispatch.acquire_slot(controller, query)
    await managed.dispatch.network_tools.acquire()
    invoke = AsyncMock()
    pending = asyncio.create_task(dispatch.invoke_tool(managed, query, invoke, "read_qq_avatar", source="plugin"))
    await until(lambda: managed.dispatch.parked == 1)
    allowed[0] = False
    managed.dispatch.network_tools.release()
    result = json.loads(await pending)
    assert result["error"] == "continuation_revoked"
    invoke.assert_not_awaited()
    assert managed.dispatch.parked == 0 and managed.dispatch.network_tools._value == 1
    dispatch.release_slot(controller, query)
    assert controller.semaphore._value == 2


@pytest.mark.asyncio
async def test_regular_explicit_tasks_do_not_inherit_read_only_policy_from_variables(managed):
    query = await put(managed, "20000", "生成图片")
    query.variables["_qq_continuation"] = True
    controller = Controller(managed)
    assert await dispatch.acquire_slot(controller, query)
    invoke = AsyncMock(return_value="explicit request still allowed")
    assert await dispatch.invoke_tool(managed, query, invoke, "generate_image", source="plugin") == "explicit request still allowed"
    invoke.assert_awaited_once()
    dispatch.release_slot(controller, query)
