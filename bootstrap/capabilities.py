"""Explicit, bounded, non-replayed AI probes. Responses never reach QQ or logs."""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import io
import json
import re
import threading
import time

from bootstrap.api import SetupError

CHECKS = {"chat", "vision", "search", "image"}


class ProbeError(ValueError):
    pass


async def _post(config, path, payload, *, maximum=1024*1024):
    import httpx
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                timeout=httpx.Timeout(60, connect=10)) as client:
        async with client.stream("POST", config["base_url"].rstrip("/")+path,
                                 headers={"Authorization": "Bearer "+config["api_key"]}, json=payload) as response:
            if not 200 <= response.status_code < 300:
                raise ProbeError(f"接口返回 HTTP {response.status_code}，请检查权限、额度及模型/接口支持。")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > maximum:
                    raise ProbeError("测试响应超过大小限制。")
    return json.loads(data)


def _red_image():
    from PIL import Image
    with Image.new("RGB", (64, 64), (255, 0, 0)) as image:
        output = io.BytesIO()
        image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


async def probe(config, kind):
    async with asyncio.timeout(95 if kind == "image" else 70):
        if kind == "search":
            from qunling.web_search import search_web
            await search_web({"api_base_url": config["base_url"], "api_key": config["api_key"],
                              "model_name": config.get("search_model") or config["chat_model"],
                              "request_timeout_seconds": 60}, "搜索 Python 官方网站并返回官网链接。")
            return "已收到真实搜索完成记录和可核对来源。"
        if kind == "chat":
            response = await _post(config, "/chat/completions", {
                "model": config["chat_model"], "messages": [{"role": "user", "content": "Call qunling_probe with ok=true."}],
                "tools": [{"type": "function", "function": {"name": "qunling_probe", "description": "Connectivity test only.",
                    "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}}}],
                "tool_choice": {"type": "function", "function": {"name": "qunling_probe"}}, "stream": False})
            calls = response["choices"][0]["message"].get("tool_calls", [])
            if (not calls or calls[0].get("function", {}).get("name") != "qunling_probe"
                    or json.loads(calls[0]["function"]["arguments"]).get("ok") is not True):
                raise ProbeError("模型未返回要求的工具调用；请确认支持工具功能。")
            return "聊天接口与工具调用通过；测试函数没有执行任何外部操作。"
        if kind == "vision":
            response = await _post(config, "/chat/completions", {
                "model": config.get("vision_model") or config["chat_model"], "stream": False,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "这张图是什么纯色？只回复颜色。"},
                    {"type": "image_url", "image_url": {"url": _red_image()}}]}]})
            text = response["choices"][0]["message"].get("content")
            if not isinstance(text, str) or not re.search(r"红|\bred\b", text, re.I):
                raise ProbeError("接口返回了响应，但没有正确识别测试色块，视觉能力尚未确认。")
            return "已正确识别实际上传的红色色块。"
        if kind == "image":
            response = await _post(config, "/images/generations", {
                "model": config["image_model"], "prompt": "A plain red square on a white background. No text.",
                "n": 1, "size": "1024x1024", "quality": "low"}, maximum=32*1024*1024)
            item = response["data"][0]
            if item.get("b64_json"):
                raw = base64.b64decode(item["b64_json"], validate=True)
            else:
                from qunling.network import fetch_public
                raw = (await fetch_public(item["url"], max_bytes=20*1024*1024, timeout=20)).body
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as image:
                if not 1 <= image.width * image.height <= 40_000_000:
                    raise ProbeError("生成结果尺寸不符合测试限制。")
                image.verify()
            return "已收到可解码的真实图片；未发送到 QQ，测试图片不保存。编辑接口需实际编辑请求另行验证。"
        raise ProbeError("不支持的测试项目。")


class Checks:
    def __init__(self, tester=probe):
        self.lock = threading.Lock()
        self.tester = tester
        self.jobs = {}
        self.running = None

    def start(self, config, values):
        if (not isinstance(values, dict) or set(values) != {"checks", "acknowledge_cost", "request_id"}
                or values["acknowledge_cost"] is not True
                or not isinstance(values["checks"], list) or not 1 <= len(values["checks"]) <= 4
                or any(not isinstance(item, str) or item not in CHECKS for item in values["checks"])
                or len(set(values["checks"])) != len(values["checks"])
                or not isinstance(values["request_id"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,64}", values["request_id"])):
            raise SetupError("请选择测试项目并确认可能产生的服务费用。")
        key = values["request_id"]
        with self.lock:
            if key in self.jobs:
                if self.jobs[key]["checks"] != values["checks"]:
                    raise SetupError("该测试编号已用于其他项目，请刷新测试状态。")
                return deepcopy(self.jobs[key])
            if self.running:
                raise SetupError("已有能力测试进行中，请等待结果，不要重复提交。")
            if len(self.jobs) >= 32:
                self.jobs.pop(next(iter(self.jobs)))
            job = {"request_id": key, "checks": list(values["checks"]), "status": "running", "results": [], "started_at": time.time()}
            self.jobs[key], self.running = job, key
            threading.Thread(target=self._run, args=(key, deepcopy(config)), daemon=True,
                             name="qunling-capability-test").start()
            return deepcopy(job)

    def _run(self, key, config):
        try:
            for kind in self.jobs[key]["checks"]:
                started = time.monotonic()
                try:
                    message = asyncio.run(self.tester(config, kind))
                    status = "passed"
                except (TimeoutError, asyncio.TimeoutError):
                    status, message = "unconfirmed", "调用超时，结果未确认；可能已计费，未自动重试。"
                except ProbeError as exc:
                    status, message = "failed", str(exc)
                except Exception as exc:
                    import httpx
                    uncertain = isinstance(exc, (httpx.TimeoutException, httpx.TransportError)) or getattr(exc, "code", "") in {
                        "search_timeout", "search_connection_failed", "search_incomplete"}
                    status, message = ("unconfirmed", "连接中断或调用未完成，结果未确认；可能已计费，未自动重试。") if uncertain else (
                        "failed", "接口连接、返回格式或能力检查未通过；未自动重试。")
                with self.lock:
                    self.jobs[key]["results"].append({"check": kind, "status": status, "message": message,
                                                     "seconds": round(time.monotonic()-started, 2)})
        finally:
            config.clear()
            with self.lock:
                self.jobs[key]["status"] = "completed"
                self.running = None

    def snapshot(self):
        with self.lock:
            return {"job": deepcopy(next(reversed(self.jobs.values()))) if self.jobs else None}
