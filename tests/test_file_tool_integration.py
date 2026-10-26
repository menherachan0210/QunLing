"""Exercise real file jobs through QQ adapters without QQ or model API calls."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langbot_plugin.api.entities.builtin.platform.message import File, MessageChain

from qunling.assets import AssetError, AssetStore
from qunling.capture import capture_message
from qunling.delivery import HostDelivery
from qunling.integration import CONTEXT_KEY, query_context
from qunling.jobs import JobService
from qunling.scope import Scope

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path / "data"))
    binding = SimpleNamespace(instance_uuid="instance", workspace_uuid="workspace", placement_generation=1)
    plugin = SimpleNamespace(get_installation_binding=lambda: binding)
    source = SimpleNamespace(
        bot_uuid="bot", launcher_type="group", launcher_id="chat", sender_id="member",
        message_chain=MessageChain([File(
            id="file-1", name="原文.txt",
            base64=base64.b64encode("这份文档需要校对。\n第二行。".encode()).decode(),
        )]),
    )
    context = capture_message(plugin, source, "query")
    scope = Scope(**context["scope"])
    api = SimpleNamespace()
    module_name = "qq_file_tool_test"
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "custom-plugins/QQTextReader/pkg/file_tools.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return SimpleNamespace(
        module=module, context=context, scope=scope, plugin=plugin, api=api,
        root=tmp_path / "data", store=AssetStore.from_env(),
    )


@pytest.mark.asyncio
async def test_upload_read_correct_export_and_send_real_bytes(env):
    module = env.module
    listing = await module.workspace(env.plugin, env.api, env.context, env.scope, env.store, {"operation": "list"})
    assert len(listing["incoming"]) == 1 and listing["incoming"][0]["current"]
    assert listing["usage"]["member"]["items"] == 1
    assert listing["usage"]["conversation"]["max_bytes"] == 256 * 1024 * 1024
    imported = await module.workspace(env.plugin, env.api, env.context, env.scope, env.store, {
        "operation": "import", "arguments": {"incoming_ref": listing["incoming"][0]["incoming_ref"]},
    })
    service = JobService(env.root)
    worker = service.worker()
    stop = asyncio.Event()
    runner = asyncio.create_task(worker.serve(stop))
    try:
        read = await module.workspace(env.plugin, env.api, env.context, env.scope, env.store, {
            "operation": "read", "arguments": {"asset_ref": imported["asset_ref"]},
        })
        assert read["status"] == "completed"
        assert "需要校对" in json.dumps(read["result"], ensure_ascii=False)
        created = await module.workspace(env.plugin, env.api, env.context, env.scope, env.store, {
            "operation": "create", "arguments": {
                "format": "txt", "filename": "校对结果.txt",
                "payload": {"text": "这份文档已完成校对。\n第二行。"},
            },
        })
        assert created["status"] == "completed"
        ref = created["result_refs"][0]
        sent = []

        async def reply(chain, **kwargs):
            sent.append(chain)

        env.api.reply = reply
        from qunling.transfers import send_attachment
        result = await send_attachment(env.store, env.scope, env.context, env.api, ref)
        assert result["status"] == "sent"
        assert len(sent) == 1
        assert base64.b64decode(sent[0].root[0].base64).decode() == "这份文档已完成校对。\n第二行。"
        assert (await send_attachment(env.store, env.scope, env.context, env.api, ref))["already_sent"]
        assert len(sent) == 1
    finally:
        stop.set()
        await runner


@pytest.mark.asyncio
async def test_chinese_ranking_xlsx_is_generated_and_delivered_as_a_real_workbook(env):
    """Cover tool -> isolated parser -> durable outbox, not just in-process export."""
    library = pytest.importorskip("openpyxl")
    source = SimpleNamespace(bot_uuid="bot", launcher_type="group", launcher_id="123456",
                             sender_id="member", message_chain=MessageChain([]))
    env.context = capture_message(env.plugin, source, "xlsx-query")
    env.scope = Scope(**env.context["scope"])
    rows = [["排名", "群友", "开工依据", "我的判断"],
            [1, "示例甲", "提出“提取→处理→打包”流程", "有明确方案"],
            [2, "示例乙", "核对卷数、更新情况\n并整理资料", "准备充分"],
            [3, "=HYPERLINK(\"https://example.invalid\")", "这是名称文本", "保留原文"]]
    jobs = JobService(env.root)
    worker = jobs.worker()
    stop = asyncio.Event()
    runner = asyncio.create_task(worker.serve(stop))
    try:
        created = await env.module.workspace(env.plugin, env.api, env.context, env.scope, env.store, {
            "operation": "create", "deliver": True,
            "arguments": {"format": "xlsx", "filename": "群友排行榜.xlsx",
                          "payload": {"sheets": [{"name": "排行榜", "rows": rows}]}},
        })
        assert created["status"] == "completed", created
        assert created["delivery_status"] == "queued"
        bot = SimpleNamespace(enable=True, adapter=SimpleNamespace(send_message=AsyncMock()))
        ap = SimpleNamespace(workspace_service=SimpleNamespace(instance_uuid="instance"))
        delivery = HostDelivery(ap, env.root, resolver=AsyncMock(return_value=bot))
        assert await delivery.tick()
        kind, chat, chain = bot.adapter.send_message.call_args.args
        assert (kind, chat) == ("group", "123456")
        attachment = chain.root[0]
        assert attachment.name == "群友排行榜.xlsx"
        raw = base64.b64decode(attachment.base64)
        assert raw == env.store.read(env.scope, created["result_refs"][0])
        workbook = library.load_workbook(io.BytesIO(raw), data_only=False)
        try:
            assert workbook.sheetnames == ["排行榜"]
            assert list(workbook.active.values) == [tuple(row) for row in rows]
            assert workbook.active["B4"].data_type == "s"
        finally:
            workbook.close()
        assert jobs.snapshot(env.scope, created["task_id"])["delivery_status"] == "sent"
        assert not await delivery.tick()
        bot.adapter.send_message.assert_awaited_once()
    finally:
        stop.set()
        await runner


@pytest.mark.asyncio
async def test_cross_plugin_context_uses_workspace_not_installation(env):
    async def get_query_vars():
        return {CONTEXT_KEY: env.context}

    async def get_bot_uuid():
        return "bot"

    api = SimpleNamespace(get_query_vars=get_query_vars, get_bot_uuid=get_bot_uuid, query_uuid="query")
    other_plugin = SimpleNamespace(get_installation_binding=lambda: SimpleNamespace(
        instance_uuid="instance", workspace_uuid="workspace", placement_generation=8,
        installation_uuid="different-plugin", runtime_revision=9,
    ))
    assert (await query_context(other_plugin, api))["scope"] == env.scope.as_dict()
    api.query_uuid = "later-query"
    with pytest.raises(Exception, match="失效"):
        await query_context(other_plugin, api)


@pytest.mark.asyncio
async def test_model_cannot_supply_scope_path_or_recipient(env):
    with pytest.raises(Exception, match="参数结构"):
        await env.module.workspace(env.plugin, env.api, env.context, env.scope, env.store, {
            "operation": "list", "arguments": {"actor_id": "someone-else"},
        })
    with pytest.raises(Exception, match="参数"):
        await env.module.workspace(env.plugin, env.api, env.context, env.scope, env.store, {
            "operation": "read", "arguments": {"path": "C:/private.txt"},
        })


@pytest.mark.asyncio
async def test_completed_read_is_not_implicitly_delivery(env):
    service = JobService(env.root)
    task = service.submit(env.scope, "create", {
        "format": "txt", "filename": "草稿.txt", "payload": {"text": "只生成，暂不发送。"},
    }, "request")
    worker = service.worker()
    await worker.run_once()
    await worker.wait_idle()
    completed = service.snapshot(env.scope, task["task_id"])
    assert completed["status"] == "completed"
    assert env.store.delivery(env.scope, "request", completed["result_refs"][0]) is None
    # Reloading the service reuses the persisted result without repeating work.
    assert JobService(env.root).snapshot(env.scope, task["task_id"])["result"] == completed["result"]


@pytest.mark.asyncio
async def test_job_status_is_scoped_and_cancellation_keeps_input(env):
    service = JobService(env.root)
    ref = env.store.list(env.scope)["assets"][0]["asset_ref"]
    task = service.submit(env.scope, "read", {"asset_ref": ref}, env.context["request_key"])
    other = Scope(env.scope.workspace_id, "bot", "group:chat", "different-member")
    with pytest.raises(Exception):
        service.snapshot(other, task["task_id"])
    result = await env.module.tasks(env.context, env.scope, {"action": "cancel", "task_id": task["task_id"]})
    assert result["status"] == "cancelled"
    assert env.store.get(env.scope, ref)


@pytest.mark.asyncio
async def test_later_same_actor_task_cannot_leak_or_grant_results_to_old_question(env):
    service = JobService(env.root)
    service.tasks.clock = lambda: env.context["created"] + 10
    later = service.submit(env.scope, "create", {
        "format": "txt", "filename": "后来请求.txt", "payload": {"text": "属于后来提问的结果。"},
    }, "later-request")
    worker = service.worker()
    await worker.run_once()
    await worker.wait_idle()
    completed = service.snapshot(env.scope, later["task_id"])
    assert completed["status"] == "completed"
    ref = completed["result_refs"][0]
    with pytest.raises(AssetError):
        env.store.require_visible(env.scope, ref, env.context)
    listed = await env.module.tasks(env.context, env.scope, {"action": "list"})
    assert later["task_id"] not in {task["task_id"] for task in listed["tasks"]}
    for action in ("status", "cancel"):
        params = {"action": action, "task_id": later["task_id"]}
        if action == "status":
            params["wait_seconds"] = 0
        with pytest.raises(AssetError):
            await env.module.tasks(env.context, env.scope, params)
    with pytest.raises(AssetError):
        env.store.require_visible(env.scope, ref, env.context)


@pytest.mark.asyncio
async def test_old_and_current_request_jobs_remain_accessible(env):
    service = JobService(env.root)
    service.tasks.clock = lambda: env.context["created"] - 10
    previous = service.submit(env.scope, "create", {
        "format": "txt", "filename": "之前任务.txt", "payload": {"text": "previous"},
    }, "earlier-request")
    service.tasks.clock = lambda: env.context["created"] + 10
    current = service.submit(env.scope, "create", {
        "format": "txt", "filename": "本次任务.txt", "payload": {"text": "current"},
    }, env.context["request_key"])
    listed = await env.module.tasks(env.context, env.scope, {"action": "list"})
    assert {task["task_id"] for task in listed["tasks"]} == {previous["task_id"], current["task_id"]}
    for task in (previous, current):
        result = await env.module.tasks(env.context, env.scope, {
            "action": "status", "task_id": task["task_id"], "wait_seconds": 0,
        })
        assert result["task_id"] == task["task_id"] and result["status"] == "pending"
