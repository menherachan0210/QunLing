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
from qunling.processing import run_operation, validate_operation
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
                                           "invalid_schema", "invalid_query", "cell_limit", "output_limit"}):
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
    validate_operation(operation, args)
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
        if operation == "create" and args.get("format") == "gif":
            from qunling.animation import source_refs
            for ref in source_refs(args["payload"]):
                store.require_visible(scope, ref, context)
    if "password" in args:
        # Encrypted inputs retain the same bounded parser and visibility rules,
        # but credentials must not enter durable payloads or task snapshots.
        try:
            result = await run_operation(store, scope, operation, args)
        except TimeoutError:
            raise AssetError("processing_timeout", "密码解压操作超时并已停止，没有创建后台任务；可用同一文件和密码重试。") from None
        for ref in result.get("result_refs", []):
            store.grant(scope, context["request_key"], ref)
        if deliver:
            ref = result["result_refs"][0]
            try:
                # Leave time within call_tool's total budget to return the
                # already-created artifact even if the QQ receipt is slow.
                async with asyncio.timeout(20):
                    delivery = await send_attachment(store, scope, context, api, ref)
            except (AssetError, TimeoutError) as error:
                uncertain = isinstance(error, TimeoutError) or error.code == "delivery_unconfirmed"
                return {**result, "status": "partial", "delivery_status": "unconfirmed" if uncertain else "failed",
                        "delivery": {"status": "unconfirmed" if uncertain else "failed",
                                     "error_code": "delivery_unconfirmed" if uncertain else error.code},
                        "next_step": ("文件已提取并保留，但 QQ 回执未确认。请先核对是否收到，不要自动重复发送。"
                                      if uncertain else "文件已提取并保留，发送失败；说明交付错误并保留返回的 asset_ref。")}
            result = {**result, "delivery_status": delivery["status"], "delivery": delivery}
        return result
    jobs = JobService(data_root())
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


async def animation(plugin, api, context, scope, store, params):
    from qunling.animation import revise_recipe, validate_recipe
    _keys(params, {"action", "recipe", "base_ref", "speed", "filename", "deliver"}, {"action"})
    action = params["action"]
    if action not in {"create", "revise", "inspect"}:
        raise AssetError("invalid_parameters", "动画操作为 create、revise 或 inspect。")
    original = None
    if action in {"revise", "inspect"}:
        ref = params.get("base_ref")
        store.require_visible(scope, ref, context)
        original = store.metadata(scope, ref).get("gif_recipe")
        if not original:
            raise AssetError("invalid_parameters", "该文件没有保存的制作方案；可将它作为图片图层重新制作。")
        original = validate_recipe(original)
    if action == "inspect":
        _keys(params, {"action", "base_ref"}, {"action", "base_ref"})
        return {"status": "ok", "recipe": original, "artifact": store.get(scope, params["base_ref"]),
                "instruction": "这是保存的制作方案，不是对成品画面的视觉判断。可直接 revise；要看动作请引用群内成品并读取图片关键帧。"}
    if action == "create":
        if "base_ref" in params or "speed" in params:
            raise AssetError("invalid_parameters", "基于已有方案修改时使用 revise。")
        recipe = validate_recipe(params.get("recipe"))
    else:
        recipe = revise_recipe(original, params.get("recipe", {}), params.get("speed", 1))
    return await workspace(plugin, api, context, scope, store, {
        "operation": "create", "deliver": params.get("deliver", True),
        "arguments": {"format": "gif", "filename": params.get("filename", "表情动画.gif"), "payload": recipe},
    })


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
            elif name == "gif_animation":
                result = await animation(tool.plugin, api, context, scope, store, params)
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
        if (name == "file_workspace" and isinstance(params, dict)
                and isinstance(params.get("arguments"), dict) and "password" in params["arguments"]):
            return _result({"status": "error", "error_code": "processing_timeout",
                            "error": "密码操作等待超时，没有创建后台任务。请先核对附件是否已送达，再决定重试。"})
        return _result({"status": "error", "error_code": "timeout",
                        "error": "本次等待超时；已提交的任务可用 file_task 查询，请勿重复提交或发送。"})
    except Exception:
        return _result({"status": "error", "error_code": "service_unavailable",
                        "error": "文件服务暂不可用；未宣称读取、处理或发送成功。"})
