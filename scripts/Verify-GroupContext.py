"""Paid synthetic group-context model check; no real group history or QQ sends.

Run with --check-only for an offline fixture/schema check without API access.
The normal run uses the existing provider key in memory, never saves it, and
performs at most five chat-model rounds per case with automatic retries disabled.
"""

from __future__ import annotations

import argparse
import asyncio
import ast
import copy
import hashlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageFont
import yaml

from local_api import LocalAPI, STATE
from chat_style import STYLE_PATH

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "custom-plugins" / "QQGroupContext"
sys.path.insert(0, str(PLUGIN_ROOT))

from langbot_plugin.api.entities.builtin.provider.message import ContentElement, Message  # noqa: E402
from pkg.context import ContextStore, QueryContext  # noqa: E402
from pkg.errors import VisionError  # noqa: E402
from pkg.media import prepare_image  # noqa: E402
from pkg import tools as context_tools  # noqa: E402

MODEL = "gpt-6.1-sol"
MAX_ROUNDS = 5
SOURCE_URLS = (
    "https://synthetic.invalid/page-a.png",
    "https://synthetic.invalid/page-b.png",
)
DEFAULT_CASES = ("discussion", "images", "greeting")
EVIDENCE_CASES = ("visual_contrast", "attribution", "ambiguous")
QUESTIONS = {
    "discussion": "他们说的几部漫画，你觉得哪部适合我？",
    "images": "上面两张介绍都说了什么？我想轻松点，哪部合适？保留原文书名。",
    "greeting": "你好",
    "visual_contrast": "锐评一下甲刚才那句话。",
    "attribution": "刚才是谁说周六要去旧书市？是我吗？",
    "ambiguous": "乙刚说“真厉害”，他是什么意思？",
}


def schemas_and_guidance():
    schemas = []
    for name in ("read_recent_group_context", "read_group_context_images"):
        document = yaml.safe_load(
            (PLUGIN_ROOT / "components" / "tools" / f"{name}.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert document["metadata"]["name"] == name
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": document["spec"]["llm_prompt"],
                    "parameters": document["spec"]["parameters"],
                },
            }
        )
    source = ast.parse(
        (PLUGIN_ROOT / "components" / "event_listener" / "context.py").read_text(
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
    return schemas, guidance


def fixture_font():
    for path in (
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ):
        if Path(path).is_file():
            return ImageFont.truetype(path, 64)
    return ImageFont.load_default(size=64)


def synthetic_images():
    fixtures = {}
    for url, color, lines in zip(
        SOURCE_URLS,
        ("#ac2735", "#194da3"),
        (
            ("SPRING NOTES", "LIGHT COMEDY", "SHORT STORIES"),
            ("IRON CHRONICLE", "DARK WAR DRAMA", "LONG SERIES"),
        ),
    ):
        with Image.new("RGB", (1200, 900), color) as picture:
            draw = ImageDraw.Draw(picture)
            draw.rectangle((35, 35, 1165, 865), outline="white", width=5)
            for y, line in zip((240, 440, 640), lines):
                draw.text(
                    (600, y), line, fill="white", font=fixture_font(), anchor="mm"
                )
            output = io.BytesIO()
            picture.save(output, format="PNG")
            fixtures[url] = prepare_image(output.getvalue())
    return fixtures


def provider_messages(messages):
    """Translate only the provider text/image elements used by this plugin."""
    converted = []
    for message in messages:
        content = message.content
        if isinstance(content, list):
            blocks = []
            for element in content:
                if element.type == "text":
                    blocks.append({"type": "text", "text": element.text or ""})
                elif element.type == "image_url" and element.image_url is not None:
                    blocks.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": element.image_url.url},
                        }
                    )
                elif element.type == "image_base64" and element.image_base64:
                    value = element.image_base64
                    if not value.startswith("data:image/"):
                        value = "data:image/jpeg;base64," + value
                    blocks.append({"type": "image_url", "image_url": {"url": value}})
                else:
                    raise ValueError("Unsupported synthetic provider content")
            content = blocks
        converted.append({"role": message.role, "content": content})
    return converted


def usage_counts(response):
    usage = getattr(response, "usage", None)
    return {
        name: int(getattr(usage, name, 0) or 0)
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
    }


def history_fixture(case, timestamp):
    def row(identifier, sender, name, text, age, images=()):
        return {
            "message_id": str(identifier),
            "sender_id": str(sender),
            "sender_name": name,
            "time": timestamp - age,
            "text": text,
            "images": [
                {"url": url, "index": index} for index, url in enumerate(images)
            ],
        }

    if case == "discussion":
        rows = [
            row(
                101,
                11,
                "虚构群友甲",
                "星海速写是轻松搞笑的短篇漫画，每个故事都能单独看。",
                120,
            ),
            row(
                102,
                22,
                "虚构群友乙",
                "灰塔编年史是沉重的长篇故事，黑暗政治和战争描写很多。",
                90,
            ),
            row(103, 33, "虚构提问者", "我最近只想看轻松的短篇，不想追长篇。", 60),
        ]
    elif case == "images":
        rows = [
            row(101, 11, "虚构群友甲", "", 120, [SOURCE_URLS[0]]),
            row(102, 22, "虚构群友乙", "", 90, [SOURCE_URLS[1]]),
            row(103, 33, "虚构提问者", "有点难选。", 60),
        ]
    elif case == "visual_contrast":
        rows = [
            row(101, 11, "虚构群友甲", "", 120, [SOURCE_URLS[0]]),
            row(102, 22, "虚构群友乙", "", 90, [SOURCE_URLS[1]]),
            row(103, 11, "虚构群友甲", "两本都是小甜饼。", 60),
        ]
    elif case == "attribution":
        rows = [
            row(
                101,
                11,
                "虚构群友甲",
                "我周六准备去旧书市找画集。",
                120,
                [SOURCE_URLS[0]],
            ),
            row(102, 22, "虚构群友乙", "我周末留在家里拼模型。", 90, [SOURCE_URLS[1]]),
            row(103, 33, "虚构提问者", "我还没有安排周末行程。", 60),
        ]
    elif case == "ambiguous":
        rows = [row(103, 22, "虚构群友乙", "真厉害。", 60)]
    else:
        rows = []
    return {"messages": rows, "window_seconds": 3600, "truncated": False}


class SyntheticPlugin:
    """Real plugin tool functions with only fixture history/image IO substituted."""

    def __init__(self, client, model_uuid, history, context, fixtures):
        self.client, self.model_uuid = client, model_uuid
        self.history, self.context, self.fixtures = history, context, fixtures
        self.contexts = ContextStore()
        self.contexts.entries["synthetic-query-token"] = context
        self.platform_reads = 0
        self.image_loads = 0
        self.vision_requests = 0
        self.vision_usage = []

    def get_config(self):
        return {"model_uuid": self.model_uuid}

    async def call_platform_api(self, bot, action, params):
        assert bot == self.context.bot and action == "call_platform_api"
        assert params == {
            "action": "get_group_recent_context",
            "params": {
                "group_id": self.context.group,
                "before_message_id": self.context.anchor,
                "before_time": self.context.timestamp,
            },
        }
        self.platform_reads += 1
        return copy.deepcopy(self.history)

    async def load_image(self, url):
        # No fallback exists: an unexpected URL can never cause a network read.
        if url not in self.fixtures:
            raise VisionError("合成图片引用无效。")
        self.image_loads += 1
        return self.fixtures[url]

    async def invoke_llm(self, *, llm_model_uuid, messages, funcs, extra_args, timeout):
        assert llm_model_uuid == self.model_uuid and funcs == [] and extra_args == {}
        self.vision_requests += 1
        response = await self.client.with_options(
            timeout=timeout
        ).chat.completions.create(
            model=MODEL,
            messages=provider_messages(messages),
            max_completion_tokens=3000,
        )
        self.vision_usage.append(usage_counts(response))
        message = response.choices[0].message
        if message.tool_calls:
            raise RuntimeError("Unexpected vision tool calls")
        return Message(role="assistant", content=message.content or "")


async def run_case(
    client, model_uuid, case, schemas, guidance, fixtures, *, style=None
):
    if style is None:
        style = STYLE_PATH.read_text(encoding="utf-8").strip()
    timestamp = int(time.time())
    context = QueryContext(
        "synthetic-owner",
        1,
        "synthetic-query",
        "synthetic-bot",
        "99",
        "33",
        "104",
        timestamp,
        time.monotonic(),
        current_question=QUESTIONS[case],
    )
    plugin = SyntheticPlugin(
        client, model_uuid, history_fixture(case, timestamp), context, fixtures
    )
    messages = [
        {
            "role": "system",
            "content": "你是群聊助手，使用简体中文自然回答，简洁说明依据。\n"
            + style
            + "\n"
            + guidance,
        },
        {"role": "user", "content": QUESTIONS[case]},
    ]
    report = {
        "case": case,
        "question": QUESTIONS[case],
        "tool_calls": [],
        "chat_usage": [],
        "chat_requests": 0,
        "answer": "",
    }
    try:
        with patch.object(context_tools, "load_image", plugin.load_image):
            for round_number in range(1, MAX_ROUNDS + 1):
                report["chat_requests"] += 1
                response = await client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    tools=schemas,
                    tool_choice="auto",
                    max_completion_tokens=3000,
                )
                report["chat_usage"].append(usage_counts(response))
                message = response.choices[0].message
                assistant = {"role": "assistant", "content": message.content}
                if message.tool_calls:
                    assistant["tool_calls"] = [
                        call.model_dump(exclude_none=True)
                        for call in message.tool_calls
                    ]
                messages.append(assistant)
                if not message.tool_calls:
                    report["answer"] = message.content or ""
                    break
                if len(message.tool_calls) > 4:
                    raise RuntimeError("Too many synthetic tool requests in one round")
                for call in message.tool_calls:
                    event = {"round": round_number, "name": call.function.name}
                    try:
                        if len(call.function.arguments) > 6000:
                            raise VisionError("合成测试参数过大。")
                        params = json.loads(call.function.arguments)
                        if not isinstance(params, dict):
                            raise VisionError("工具参数必须是对象。")
                        event["arguments"] = params
                        if call.function.name == "read_recent_group_context":
                            result = await context_tools.read_context(
                                plugin, context, params
                            )
                        elif call.function.name == "read_group_context_images":
                            result = await context_tools.read_images(
                                plugin, context, params
                            )
                        else:
                            raise VisionError("合成测试没有此工具。")
                    except VisionError as exc:
                        result = {"status": "error", "error": str(exc)}
                    except Exception as exc:
                        result = {"status": "error", "error_type": type(exc).__name__}
                    event["result"] = result
                    report["tool_calls"].append(event)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
    except Exception as exc:
        # Provider exceptions can contain headers or URLs: record only the class.
        report["error_type"] = type(exc).__name__
    report.update(
        {
            "platform_history_reads": plugin.platform_reads,
            "synthetic_image_loads": plugin.image_loads,
            "vision_requests": plugin.vision_requests,
            "vision_usage": plugin.vision_usage,
        }
    )
    successful = [
        call for call in report["tool_calls"] if call["result"].get("status") == "ok"
    ]
    context_calls = [
        call for call in successful if call["name"] == "read_recent_group_context"
    ]
    image_calls = [
        call for call in successful if call["name"] == "read_group_context_images"
    ]
    answer = report["answer"]
    checks = {
        "answered": bool(answer.strip()),
        "bounded_chat_rounds": report["chat_requests"] <= MAX_ROUNDS,
        "at_most_one_vision_request": plugin.vision_requests <= 1,
    }
    if case == "discussion":
        checks.update(
            {
                "read_context_naturally": bool(context_calls),
                "single_history_snapshot": plugin.platform_reads == 1,
                "no_unneeded_vision": plugin.vision_requests == 0,
                "recommended_light_short_work": "星海速写" in answer
                and ("轻松" in answer or "短篇" in answer),
                "identified_questioner": bool(context_calls)
                and any(
                    message.get("is_questioner") and "只想" in message.get("text", "")
                    for call in context_calls
                    for message in call["result"]["messages"]
                ),
            }
        )
    elif case == "images":
        checks.update(
            {
                "read_context_before_image_round": bool(context_calls and image_calls)
                and context_calls[0]["round"] < image_calls[0]["round"],
                "two_real_pixels_analyzed": plugin.image_loads == 2
                and plugin.vision_requests == 1
                and bool(image_calls),
                "retained_original_titles": "SPRING NOTES" in answer.upper()
                and "IRON CHRONICLE" in answer.upper(),
                "explained_light_option": "SPRING NOTES" in answer.upper()
                and any(word in answer for word in ("轻松", "喜剧", "短篇")),
                "explained_heavy_option": any(
                    word in answer for word in ("黑暗", "沉重", "长篇")
                ),
            }
        )
    elif case == "greeting":
        checks.update(
            {
                "no_history_for_greeting": not report["tool_calls"]
                and plugin.platform_reads == 0,
                "no_vision_for_greeting": plugin.vision_requests == 0,
            }
        )
    elif case == "visual_contrast":
        checks.update(
            {
                "read_context_before_vision": bool(context_calls and image_calls)
                and context_calls[0]["round"] < image_calls[0]["round"],
                "two_images_read_once": plugin.image_loads == 2
                and plugin.vision_requests == 1,
                "single_history_snapshot": plugin.platform_reads == 1,
            }
        )
    elif case == "attribution":
        checks.update(
            {
                "read_context": bool(context_calls),
                "single_history_snapshot": plugin.platform_reads == 1,
                "no_unneeded_vision": plugin.vision_requests == 0
                and plugin.image_loads == 0,
            }
        )
    elif case == "ambiguous":
        checks.update(
            {
                "checked_missing_context": plugin.platform_reads == 1,
                "no_unneeded_vision": plugin.vision_requests == 0
                and plugin.image_loads == 0,
            }
        )
    report["checks"] = checks
    report["routing_passed"] = all(checks.values()) and "error_type" not in report
    report["usage_total"] = {
        key: sum(
            usage.get(key, 0) for usage in report["chat_usage"] + plugin.vision_usage
        )
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    if case in EVIDENCE_CASES:
        report["semantic_review"] = {
            "status": "pending_semantic_review",
            "criteria": {
                "visual_contrast": "结合一轻松短篇、一沉重战争长篇识别原话与画面的反差；评价甲的说法，不改写成建议、不给提问者安上该观点。",
                "attribution": "明确去旧书市的是甲而不是本轮提问者；不把无关图片拉进解释。",
                "ambiguous": "资料不足以确定赞扬还是反话，保留歧义或简短询问，不编造前情和动机。",
            }[case],
        }
        report["passed"] = None  # Routing assertions do not grade meaning.
    else:
        report["passed"] = report["routing_passed"]
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate synthetic inputs offline; no credentials or paid calls.",
    )
    parser.add_argument(
        "--case",
        choices=["all", "evidence", *DEFAULT_CASES, *EVIDENCE_CASES],
        default="all",
    )
    args = parser.parse_args()
    cases = (
        list(DEFAULT_CASES)
        if args.case == "all"
        else list(EVIDENCE_CASES)
        if args.case == "evidence"
        else [args.case]
    )
    schemas, guidance = schemas_and_guidance()
    style = STYLE_PATH.read_text(encoding="utf-8").strip()
    fixtures = synthetic_images()
    if args.check_only:
        assert len(schemas) == 2 and isinstance(guidance, str) and guidance
        assert len(fixtures) == 2 and all(
            value.startswith("data:image/jpeg;base64,") for value in fixtures.values()
        )
        assert all(len(value) <= 2 * 1024 * 1024 for value in fixtures.values())
        converted = provider_messages(
            [
                Message(
                    role="user",
                    content=[
                        ContentElement.from_image_url(next(iter(fixtures.values())))
                    ],
                )
            ]
        )
        assert converted[0]["content"][0]["type"] == "image_url"
        assert style
        assert "message_refs" in schemas[1]["function"]["parameters"]["properties"]
        for case in cases:
            assert QUESTIONS[case]
            history = history_fixture(case, 2_000_000_000)
            assert all(row["time"] < 2_000_000_000 for row in history["messages"])
        print(
            json.dumps(
                {
                    "offline_check": "passed",
                    "tool_schemas": 2,
                    "synthetic_images": 2,
                    "paid_calls": 0,
                    "cases": cases,
                    "current_chat_style": True,
                }
            )
        )
        return 0

    report = {
        "fixture": "entirely synthetic three-person discussion and two English book-introduction images",
        "model": MODEL,
        "platform_history_mocked": True,
        "image_downloads_mocked": True,
        "real_user_history_used": False,
        "qq_messages_sent_by_test": False,
        "real_model_calls": True,
        "automatic_retries": 0,
        "maximum_chat_rounds_per_case": MAX_ROUNDS,
        "chat_style_sha256": hashlib.sha256(style.encode()).hexdigest(),
        "guidance_sha256": hashlib.sha256(guidance.encode()).hexdigest(),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "cases": [],
    }
    report_path = STATE / (
        "group-context-evidence-verification.json"
        if any(case in EVIDENCE_CASES for case in cases)
        else "group-context-verification.json"
    )

    def save():
        STATE.mkdir(exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    try:
        if not (STATE / "langbot-admin.json").is_file():
            raise RuntimeError("Existing local administrator is required")
        state = json.loads((STATE / "setup-state.json").read_text(encoding="utf-8"))
        api = LocalAPI()
        try:
            provider = api.call(
                "GET",
                "/api/v1/provider/providers/"
                + state["provider_uuid"]
                + "?include_secret=true",
            )["provider"]
        finally:
            api.http.close()
        if not provider.get("api_keys") or not provider.get("base_url"):
            raise RuntimeError("Existing model provider is not configured")
        # Credentials and endpoint remain in memory and are absent from the report.
        async with httpx.AsyncClient(trust_env=False, timeout=150) as transport:
            async with AsyncOpenAI(
                api_key=provider["api_keys"][0],
                base_url=provider["base_url"],
                max_retries=0,
                timeout=150,
                http_client=transport,
            ) as client:
                for case in cases:
                    result = await run_case(
                        client,
                        state["models"][MODEL],
                        case,
                        schemas,
                        guidance,
                        fixtures,
                        style=style,
                    )
                    report["cases"].append(result)
                    save()
                    print(
                        json.dumps(
                            {
                                "case": case,
                                "routing_passed": result["routing_passed"],
                                "chat_requests": result["chat_requests"],
                                "vision_requests": result["vision_requests"],
                                "tool_route": [
                                    call["name"] for call in result["tool_calls"]
                                ],
                                "answer": result["answer"],
                                "usage": result["usage_total"],
                            },
                            ensure_ascii=False,
                        )
                    )
        report["routing_passed"] = all(
            case["routing_passed"] for case in report["cases"]
        )
        report["passed"] = (
            None
            if any(case["passed"] is None for case in report["cases"])
            else all(case["passed"] for case in report["cases"])
        )
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        report["passed"] = False
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    save()
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "synthetic_cases": len(report["cases"]),
                "report": ".local/" + report_path.name,
                "routing_passed": report.get("routing_passed", False),
                "qq_messages_sent": 0,
            }
        )
    )
    return 0 if report.get("routing_passed") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
