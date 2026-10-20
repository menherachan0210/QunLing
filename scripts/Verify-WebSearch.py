"""Real public-web/model checks with a fake QQ transport; no group messages.

Costs API credit. --case selects one test. Public content is used, never QQ logs.
Search/vision failures are recorded as failures, not treated as working image delivery.
"""

from __future__ import annotations

import argparse
import asyncio
import ast
import base64
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI
import yaml

from local_api import LocalAPI, STATE

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "custom-plugins/QQWebSearch"
sys.path.insert(0, str(PLUGIN))

from pkg.context import QueryStore  # noqa: E402
from pkg.tools import execute  # noqa: E402

NAMES = {
    "search_web": "search",
    "read_web_page": "page",
    "search_web_images": "images",
    "send_web_images": "send",
    "list_web_files": "file_links",
    "download_web_file": "file_download",
    "send_web_file": "file_send",
}
CASES = {
    "search": "联网查一下 Python 的官方文档入口在哪里，附上来源链接。",
    "page": "读一下 https://www.python.org/about/ ，用两句话说这个页面介绍了什么。",
    "images": "找一张帝企鹅的真实照片发给我，优先公开可访问的官方页面或维基共享资源。",
    "greeting": "早上好呀。",
    "font_attachment": (
        "帮我找作者amazusa的「瀞ノグリッチ黒体 H1」字体，"
        "把H1字体文件直接作为附件传过来，不是整仓库压缩包。"
    ),
    "source_attachment": (
        "把这个公开源码文件下载后作为附件发给我："
        "https://raw.githubusercontent.com/python/cpython/main/Lib/this.py ，"
        "我只要文件，不需要运行或讲解代码。"
    ),
}


def definitions():
    tools = []
    for name in NAMES:
        doc = yaml.safe_load(
            (PLUGIN / f"components/tools/{name}.yaml").read_text(encoding="utf-8")
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": doc["spec"]["llm_prompt"],
                    "parameters": doc["spec"]["parameters"],
                },
            }
        )
    source = ast.parse(
        (PLUGIN / "components/event_listener/web_context.py").read_text(
            encoding="utf-8"
        )
    )
    guidance = next(
        ast.literal_eval(node.value)
        for node in source.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "GUIDANCE"
            for target in node.targets
        )
    )
    return tools, guidance


class FakeAPI:
    def __init__(self):
        self.variables = {}
        self.sent = []

    async def get_query_vars(self):
        return self.variables.copy()

    async def set_query_var(self, key, value):
        self.variables[key] = value

    async def reply(self, chain, **kwargs):
        # Inspect actual bytes locally; no real platform method is reachable.
        self.sent.append(
            {
                "image_count": sum(item.type == "Image" for item in chain),
                "source_count": sum(
                    item.type == "Plain" and "https://" in item.text for item in chain
                ),
                "files": [
                    {
                        "name": item.name,
                        "size": len(base64.b64decode(item.base64, validate=True)),
                        "sha256": hashlib.sha256(
                            base64.b64decode(item.base64, validate=True)
                        ).hexdigest(),
                    }
                    for item in chain
                    if item.type == "File"
                ],
            }
        )


class TestPlugin:
    def __init__(self, client, config):
        self.client, self.config = client, config
        self.web_queries = QueryStore()
        self.plugin_runtime_handler = None
        self.vision_calls = 0

    def get_config(self):
        return self.config

    async def invoke_llm(self, *, messages, **kwargs):
        self.vision_calls += 1
        converted = []
        for message in messages:
            content = message.content
            if isinstance(content, list):
                content = [
                    {"type": "text", "text": item.text}
                    if item.type == "text"
                    else {"type": "image_url", "image_url": {"url": item.image_url.url}}
                    for item in content
                ]
            converted.append({"role": message.role, "content": content})
        response = await self.client.chat.completions.create(
            model="gpt-6.1-sol",
            messages=converted,
        )
        return response.choices[0].message


async def run_case(client, config, prompts, schemas, guidance, name):
    plugin, transport = TestPlugin(client, config), FakeAPI()
    messages = prompts + [
        {"role": "system", "content": guidance},
        {"role": "user", "content": CASES[name]},
    ]
    calls, results, answer = [], [], ""
    for _ in range(8 if name.endswith("_attachment") else 4):
        response = await client.chat.completions.create(
            model="gpt-6.1-sol",
            messages=messages,
            tools=schemas,
        )
        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))
        if not message.tool_calls:
            answer = message.content or ""
            break
        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name
            calls.append(tool_name)
            with patch("pkg.tools.QueryBasedAPIProxy", return_value=transport):
                result_text = await execute(
                    SimpleNamespace(plugin=plugin),
                    NAMES.get(tool_name),
                    json.loads(tool_call.function.arguments),
                    101,
                    "synthetic-" + name,
                )
            result = json.loads(result_text)
            results.append(
                {
                    "tool": tool_name,
                    "status": result.get("status"),
                    "error": result.get("error"),
                    "source_count": len(result.get("sources", [])),
                    "candidate_count": result.get("checked_candidates"),
                    "match_count": len(result.get("images", [])),
                }
            )
            messages.append(
                {"role": "tool", "tool_call_id": tool_call.id, "content": result_text}
            )
    passed = bool(answer)
    if name == "greeting":
        passed = passed and not calls
    elif name == "search":
        passed = passed and any(
            r["tool"] == "search_web" and r["status"] == "ok" for r in results
        )
        passed = passed and "http" in answer
    elif name == "page":
        passed = passed and any(
            r["tool"] == "read_web_page" and r["status"] == "ok" for r in results
        )
    elif name.endswith("_attachment"):
        captured = [item for entry in transport.sent for item in entry["files"]]
        # Tool replies already deliver an attachment. A second plain-text
        # acknowledgement is optional and may legitimately be empty.
        passed = len(captured) == 1 and captured[0]["size"] > 0
        passed = passed and any(
            r["tool"] == "send_web_file" and r["status"] == "sent" for r in results
        )
        if name == "font_attachment" and captured:
            passed = passed and "h1" in captured[0]["name"].lower()
            passed = passed and captured[0]["name"].lower().endswith((".otf", ".ttf"))
        if name == "source_attachment" and captured:
            passed = passed and captured[0]["name"] == "this.py"
    else:
        passed = passed and any(
            r["tool"] == "send_web_images" and r["status"] == "sent" for r in results
        )
        passed = (
            passed
            and bool(transport.sent)
            and all(
                entry["image_count"] == entry["source_count"]
                for entry in transport.sent
            )
        )
    return {
        "case": name,
        "passed": passed,
        "calls": calls,
        "results": results,
        "answer": answer,
        "captured_sends": transport.sent,
        "vision_calls": plugin.vision_calls,
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=["all", *CASES], default="all")
    args = parser.parse_args()
    state = json.loads((STATE / "setup-state.json").read_text(encoding="utf-8"))
    api = LocalAPI()
    provider = api.call(
        "GET",
        "/api/v1/provider/providers/" + state["provider_uuid"] + "?include_secret=true",
    )["provider"]
    config = api.call("GET", "/api/v1/pipelines/" + state["pipeline_uuid"])["pipeline"][
        "config"
    ]
    prompts = config["ai"]["runner_config"]["plugin:langbot-team/LocalAgent/default"][
        "prompt"
    ]
    plugin_config = {
        "api_base_url": provider["base_url"],
        "api_key": provider["api_keys"][0],
        "model_name": "gpt-6.1-sol",
        "request_timeout_seconds": 65,
        "vision_model_uuid": state["models"]["gpt-6.1-sol"],
    }
    schemas, guidance = definitions()
    report = {
        "qq_messages_sent": 0,
        "real_model_calls": True,
        "public_web_only": True,
        "cases": [],
    }
    async with AsyncOpenAI(
        api_key=provider["api_keys"][0],
        base_url=provider["base_url"],
        max_retries=0,
        timeout=90,
        http_client=httpx.AsyncClient(trust_env=False),
    ) as client:
        for case in CASES if args.case == "all" else [args.case]:
            try:
                row = await run_case(
                    client, plugin_config, prompts, schemas, guidance, case
                )
            except Exception as exc:
                row = {"case": case, "passed": False, "error_type": type(exc).__name__}
            report["cases"].append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            filename = "web-search-verification-" + args.case + ".json"
            (STATE / filename).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    return 0 if all(row["passed"] for row in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
