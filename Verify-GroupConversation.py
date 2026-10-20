"""Synthetic speaker/topic regression check using the configured chat provider.

Uses the installed LocalAgent history decoder and patched Host projection.
No real QQ history is read and no QQ messages are sent. A live run makes one
paid model request per case, without retries. --check-only performs no API calls.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import httpx
from openai import AsyncOpenAI

from chat_style import styled_prompts
from local_api import LocalAPI, STATE

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "langbot/src"))

# Match the Host entry point's import order (the plugin handler imports app).
from langbot.pkg.core import app as _host_app  # noqa: E402,F401
from langbot.pkg.plugin.agent_pull_actions import _project_group_history_items  # noqa: E402
from langbot_plugin.api.entities.builtin.provider.message import Message  # noqa: E402


def history_item(role, content, actor_id=None):
    return {
        "role": role,
        "event_id": "synthetic-history-event",
        "content": content,
        "content_json": {"role": role, "content": content},
        "metadata": {"actor_type": "user", "actor_id": actor_id} if actor_id else {},
    }


HISTORY = [
    history_item("user", "给我报销键盘的钱880吧。", "synthetic-member-a"),
    history_item("assistant", "键盘预算880，报销窗口余额0，先给你盖个想得美章。"),
]
CASES = [
    {
        "name": "new_topic_other_member",
        "actor_id": "synthetic-member-b",
        "question": "这个编辑器自动补全真好用。",
        "history": HISTORY,
        "criteria": "自然回应编辑器，不强拉键盘报销旧梗，不把甲的购买归给乙。",
    },
    {
        "name": "new_topic_same_member",
        "actor_id": "synthetic-member-a",
        "question": "我今天终于把跑步的五公里目标完成了！",
        "history": HISTORY,
        "criteria": "回应跑步进展；即使同一人换话题，也不硬接键盘报销旧梗。",
    },
    {
        "name": "explicit_callback_other_member",
        "actor_id": "synthetic-member-b",
        "question": "刚才有人要报销键盘，多少钱来着？是我说的吗？",
        "history": HISTORY,
        "criteria": "回答880，并明确是另一位成员提出，不说成当前人的要求。",
    },
    {
        "name": "explicit_callback_same_member",
        "actor_id": "synthetic-member-a",
        "question": "我刚才让你报销什么？多少钱？",
        "history": HISTORY,
        "criteria": "正确延续本人的历史：键盘、880；不能因身份保护而失去可用记忆。",
    },
    {
        "name": "unknown_legacy_speaker",
        "actor_id": "synthetic-member-b",
        "question": "有人买了键盘要报销，是我吗？",
        "history": [
            history_item("user", "给我报销键盘的钱880吧。"),
            history_item("assistant", "你的键盘预算先记上了。"),
        ],
        "criteria": "不能确定是当前人，不把历史助手的“你的”当成身份证据。",
    },
    {
        "name": "different_members_different_preferences",
        "actor_id": "synthetic-member-b",
        "question": "按我刚才说的口味，这两本你给我选哪本？",
        "history": [
            history_item(
                "user", "我喜欢悲剧。甲是轻松喜剧，乙是沉重悲剧。", "synthetic-member-a"
            ),
            history_item("assistant", "按这个口味我选乙。"),
            history_item("user", "我想放松，更喜欢轻松喜剧。", "synthetic-member-b"),
            history_item("assistant", "那甲更适合你。"),
        ],
        "criteria": "按当前成员乙的轻松喜剧口味选甲，不套用成员甲的悲剧偏好。",
    },
]


def project_history(case):
    session = {
        "authorization": {
            "platform_context": {
                "actor": {"actor_type": "user", "actor_id": case["actor_id"]},
                "subject": {
                    "subject_type": "message",
                    "data": {"launcher_type": "group"},
                },
                "delivery": {
                    "reply_target": {
                        "target_type": "group",
                        "target_id": "synthetic-group",
                    }
                },
            }
        }
    }
    return _project_group_history_items(case["history"], session)


def runner_modules(api):
    plugins = api.call("GET", "/api/v1/plugins")["plugins"]
    runner = next(
        item
        for item in plugins
        if item["manifest"]["manifest"]["metadata"]["name"] == "LocalAgent"
        and item["manifest"]["manifest"]["metadata"]["author"] == "langbot-team"
    )
    digest = runner["install_info"]["_certification"]["artifact_digest"]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid installed runner digest")
    source = ROOT / "langbot/data/plugin-runtime/artifacts/sha256" / digest / "code"
    sys.path.insert(0, str(source))
    return (
        importlib.import_module("pkg.context_pipeline"),
        importlib.import_module("pkg.messages"),
    )


def current_event(case, runner_messages):
    actor = SimpleNamespace(
        model_dump=lambda **_: {
            "actor_type": "user",
            "actor_id": case["actor_id"],
            "actor_name": "同名成员",
        }
    )
    context = SimpleNamespace(
        event=SimpleNamespace(
            event_type="message.received",
            source="platform:qq",
            data={"launcher_type": "group"},
        ),
        actor=actor,
        subject=None,
    )
    return runner_messages.build_event_context_message(context)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--case", choices=[case["name"] for case in CASES])
    args = parser.parse_args()
    cases = [case for case in CASES if not args.case or case["name"] == args.case]
    for case in cases:
        projected = project_history(case)
        assert projected != case["history"]
        for item in projected:
            Message.model_validate(item["content_json"])
        assert all(
            "Group history speaker:" not in item["content"] for item in case["history"]
        )
    if args.check_only:
        print(
            json.dumps(
                {
                    "synthetic_cases": len(cases),
                    "api_requests": 0,
                    "qq_messages_sent": 0,
                }
            )
        )
        return

    api = LocalAPI()
    state = json.loads((STATE / "setup-state.json").read_text(encoding="utf-8"))
    pipeline = api.call("GET", "/api/v1/pipelines/" + state["pipeline_uuid"])[
        "pipeline"
    ]
    config = pipeline["config"]["ai"]["runner_config"][
        "plugin:langbot-team/LocalAgent/default"
    ]
    prompts = styled_prompts(config["prompt"])
    provider = api.call(
        "GET",
        "/api/v1/provider/providers/" + state["provider_uuid"] + "?include_secret=true",
    )["provider"]
    context_pipeline, runner_messages = runner_modules(api)
    semaphore = asyncio.Semaphore(2)
    async with AsyncOpenAI(
        api_key=provider["api_keys"][0],
        base_url=provider["base_url"],
        max_retries=0,
        timeout=75,
        http_client=httpx.AsyncClient(trust_env=False),
    ) as client:

        async def run(case):
            async with semaphore:
                history = [
                    context_pipeline._message_from_transcript_item(
                        item, "synthetic-current-event"
                    )
                    for item in project_history(case)
                ]
                assert all(message is not None for message in history)
                messages = (
                    prompts
                    + [
                        {"role": message.role, "content": message.content}
                        for message in history + [current_event(case, runner_messages)]
                    ]
                    + [{"role": "user", "content": case["question"]}]
                )
                try:
                    response = await client.chat.completions.create(
                        model="gpt-6.1-sol", messages=messages
                    )
                    result = {
                        "case": case["name"],
                        "criteria": case["criteria"],
                        "answer": response.choices[0].message.content,
                        "finish_reason": response.choices[0].finish_reason,
                    }
                except Exception as exc:
                    # Do not print request bodies, provider URLs or credentials.
                    result = {"case": case["name"], "error_type": type(exc).__name__}
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return result

        results = await asyncio.gather(*(run(case) for case in cases))
    report = {
        "synthetic_only": True,
        "model": "gpt-6.1-sol",
        "live_style_matches": prompts == config["prompt"],
        "uses_installed_runner_history_decoder": True,
        "qq_messages_sent": 0,
        "provider_requests": len(cases),
        "cases": results,
    }
    path = STATE / "group-conversation-verification.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if any("error_type" in result for result in results):
        raise SystemExit("Some synthetic API checks failed; see the local report.")


if __name__ == "__main__":
    asyncio.run(main())
