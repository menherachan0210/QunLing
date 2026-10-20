"""Query-bound adapters for the shared file store and durable processing service."""
from __future__ import annotations

import asyncio
import json

from langbot_plugin.api.proxies.query_based_api import QueryBasedAPIProxy

from qunling.assets import AssetError, AssetStore, data_root
from qunling.inbox import Inbox
from qunling.integration import query_context
from qunling.jobs import JobService
from qunling.net_errors import NetworkError
from qunling.scope import Scope
from qunling.transfers import import_attachment, send_attachment


def _keys(params, allowed, required=()):
    if not isinstance(params, dict) or set(params) - set(allowed) or set(required) - set(params):
        raise AssetError("invalid_parameters", "参数结构不符合操作要求，请检查工具定义后继续。")


def _result(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _task_result(value, store=None, scope=None, context=None):
    if store is not None and value.get("status") == "completed":
        for ref in value.get("result_refs", []):
            store.grant(scope, context["request_key"], ref)
    retry_wait = value.get("status") == "waiting" and value.get("retryable") is True
    if value.get("status") in {"pending", "running"} or retry_wait:
        value = {**value, "_qunling_task": {
            "version": 1, "task_id": value.get("task_id", value.get("id")),
            "status": value["status"], "can_continue": True, "retryable": False,
            "error_code": None, "next_action": "continue",
        }, "next_step": "继续调用 file_task 查询该任务，无需让用户再次同意；若本轮已用尽预算，如实告知任务仍在后台处理。"}
    detail = value.get("result")
    if (value.get("status") == "failed" and isinstance(detail, dict)
            and detail.get("error_code") in {"invalid_selector", "invalid_parameters", "result_limit",
                                           "invalid_schema", "invalid_query", "cell_limit"}):
        value = {**value, "_qunling_task": {
            "version": 1, "task_id": value.get("task_id"), "status": "partial",
            "can_continue": True, "retryable": False, "error_code": "invalid_input", "next_action": "continue",
        }, "next_step": "这是可纠正的参数或范围错误，按具体原因调整 selector/列范围/生成数据后继续，不必让用户重新确认。"}
    return value


async def workspace(plugin, api, context, scope, store, params):
    _keys(params, {"operation", "arguments", "deliver"}, {"operation"})
    operation, args = params["operation"], params.get("arguments", {})
    deliver = params.get("deliver", False)
    if type(deliver) is not bool or (deliver and operation not in {"create", "extract"}):
        raise AssetError("invalid_parameters", "deliver 只用于生成或提取后的文件交付。")
    if operation == "list":
        _keys(args, {"cursor"})
        incoming = Inbox(store).list(scope, before=context["created"],
                                    include_refs=context["inbox_refs"], limit=100)
        current = set(context["inbox_refs"])
        for entry in incoming:
            entry["current"] = entry["incoming_ref"] in current
        incoming.sort(key=lambda entry: not entry["current"])
        return {
            "status": "ok", "incoming": incoming,
            **store.list(scope, cursor=args.get("cursor"), context=context),
            "unresolved_quote": context["unresolved_quote"],
            "warnings": context.get("warnings", []),
            "instruction": "incoming_ref 需 import 后使用；asset_ref 可直接读取或发送。只选择与用户请求匹配的文件。",
        }
    if operation == "import":
        _keys(args, {"incoming_ref"}, {"incoming_ref"})
        item = await import_attachment(plugin, store, scope, args["incoming_ref"], before=context["created"],
                                       current_refs=context["inbox_refs"])
        store.grant(scope, context["request_key"], item["asset_ref"])
        return {"status": "imported", **item, "next_step": "使用 asset_ref 检查、阅读或发送；导入不表示已解析正文。"}
    if operation == "delete":
        _keys(args, {"asset_ref"}, {"asset_ref"})
        store.require_visible(scope, args["asset_ref"], context)
        store.delete(scope, args["asset_ref"])
        return {"status": "deleted"}
    jobs = JobService(data_root())
    if isinstance(args, dict):
        if "asset_ref" in args:
            store.require_visible(scope, args["asset_ref"], context)
        if operation == "create" and args.get("format") == "zip":
            payload = args.get("payload")
            entries = payload.get("files", []) if isinstance(payload, dict) else []
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and "asset_ref" in entry:
                        store.require_visible(scope, entry["asset_ref"], context)
    submitted = jobs.submit(scope, operation, args, context["request_key"])
    task_id = submitted.get("task_id", submitted.get("id"))
    if deliver:
        jobs.attach_delivery(scope, task_id, context["request_key"], context.get("host_route"))
    result = await jobs.wait(
        scope, task_id, seconds=15,
        before=context["created"], request_key=context["request_key"],
    )
    return _task_result(result, store, scope, context)


async def tasks(context, scope, params):
    _keys(params, {"action", "task_id", "wait_seconds"}, {"action"})
    jobs = JobService(data_root())
    action = params["action"]
    if action == "list":
        _keys(params, {"action"}, {"action"})
        return {"status": "ok", "tasks": jobs.list(
            scope, before=context["created"], request_key=context["request_key"],
        )}
    if "task_id" not in params:
        raise AssetError("invalid_parameters", "需要工具先前返回的 task_id。")
    if action == "cancel":
        _keys(params, {"action", "task_id"}, {"action", "task_id"})
        return jobs.cancel(
            scope, params["task_id"],
            before=context["created"], request_key=context["request_key"],
        )
    if action == "status":
        seconds = params.get("wait_seconds", 10)
        if type(seconds) is not int or not 0 <= seconds <= 15:
            raise AssetError("invalid_parameters", "wait_seconds 应为 0 至 15。")
        return _task_result(await jobs.wait(
            scope, params["task_id"], seconds=seconds,
            before=context["created"], request_key=context["request_key"]),
                            AssetStore.from_env(), scope, context)
    raise AssetError("invalid_parameters", "未知任务操作。")


async def call_tool(tool, name, params, query_id, query_uuid):
    try:
        api = QueryBasedAPIProxy(query_id=query_id, query_uuid=query_uuid,
                                 plugin_runtime_handler=tool.plugin.plugin_runtime_handler)
        context = await query_context(tool.plugin, api)
        scope = Scope(**context["scope"])
        store = AssetStore.from_env()
        async with asyncio.timeout(90):
            if name == "file_workspace":
                result = await workspace(tool.plugin, api, context, scope, store, params)
            elif name == "file_task":
                result = await tasks(context, scope, params)
            elif name == "send_attachment":
                _keys(params, {"asset_ref"}, {"asset_ref"})
                result = await send_attachment(store, scope, context, api, params["asset_ref"])
            else:
                raise AssetError("invalid_parameters", "未知文件工具。")
        return _result(result)
    except (AssetError, NetworkError) as error:
        result = {"status": "error", "error_code": error.code, "error": str(error)}
        # Safe corrective actions may continue, but no automatic repeat of an
        # upload with an ambiguous receipt, or an operation lacking permission.
        if error.code in {"invalid_parameters", "invalid_selector", "result_limit"}:
            result["_qunling_task"] = {
                "version": 1, "status": "partial", "can_continue": True,
                "retryable": False, "error_code": "invalid_input", "next_action": "continue",
            }
        return _result(result)
    except TimeoutError:
        return _result({"status": "error", "error_code": "timeout",
                        "error": "本次等待超时；已提交的任务可用 file_task 查询，请勿重复提交或发送。"})
    except Exception:
        return _result({"status": "error", "error_code": "service_unavailable",
                        "error": "文件服务暂不可用；未宣称读取、处理或发送成功。"})
