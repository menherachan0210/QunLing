"""Paid synthetic chat-routing check; no real group history or QQ delivery.

The chat model uses real plugin prompts, selection and tool guards. The image
provider and QQ reply are replaced with recorders. A separate image-provider
probe is needed to verify rendered output. At most four chat calls per case.
"""

from __future__ import annotations

import asyncio
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from openai import AsyncOpenAI
import yaml

from local_api import LocalAPI, STATE

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "custom-plugins/AIImagePlugin"
sys.path.insert(0, str(PLUGIN))
from langbot_plugin.api.entities.builtin.platform.message import (  # noqa: E402
    Image,
    MessageChain,
    Plain,
    Source,
)
from langbot_plugin.api.entities.builtin.provider.message import Message  # noqa: E402
from components.event_listener import image_context as listener_module  # noqa: E402
from components.tools import edit_image as edit_module  # noqa: E402
from components.tools import list_edit_image_references as list_module  # noqa: E402
from main import GeneratedImage  # noqa: E402
from pkg import edit_references as refs  # noqa: E402
from pkg.image_context import QUERY_CONTEXT_KEY, capture_context  # noqa: E402

URLS = ["https://synthetic.invalid/first.png", "https://synthetic.invalid/second.png"]
CASES = [
    "two_current_images",
    "other_member_previous_images",
    "same_sender_separate_uploads",
    "followup_after_old_single_image_reply",
]
SYNTHETIC_RESULT = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aCJ8AAAAASUVORK5CYII="


async def run_case(client, prompts, case, index):
    now = int(datetime.now(timezone.utc).timestamp())
    query_id = 800 + index
    query_uuid = f"synthetic-{query_id}"
    plugin = SimpleNamespace(
        plugin_runtime_handler=object(),
        call_platform_api=AsyncMock(return_value={"messages": []}),
        edit_image=AsyncMock(return_value=GeneratedImage(base64=SYNTHETIC_RESULT)),
    )
    question = "把上面两张图合成一张，第一张放左边，第二张放右边，保留各自内容。"

    def query(images, *, number=query_id, kind="group", timestamp=now):
        return SimpleNamespace(
            query_id=number,
            query_uuid=f"synthetic-{number}",
            bot_uuid="synthetic-bot",
            launcher_type=kind,
            launcher_id="201",
            sender_id="101",
            message_chain=MessageChain(
                [
                    Source(
                        id=str(number),
                        time=datetime.fromtimestamp(timestamp, timezone.utc),
                    ),
                    *images,
                    Plain(text=question),
                ]
            ),
        )

    refs.STORE = refs.ReferenceStore()
    if case in {"two_current_images", "followup_after_old_single_image_reply"}:
        source = query([Image(url=url) for url in URLS])
    elif case == "same_sender_separate_uploads":
        for number, url in enumerate(URLS):
            refs.capture_references(
                plugin,
                query(
                    [Image(url=url)],
                    number=700 + number,
                    kind="person",
                    timestamp=now - 2 + number,
                ),
            )
        source = query([], kind="person")
    else:
        source = query([])
        plugin.call_platform_api.return_value = {
            "messages": [
                {
                    "message_id": str(501 + i),
                    "time": now - 3 + i,
                    "sender_id": "102",
                    "sender_name": "虚构群友",
                    "text": f"参考图{i + 1}",
                    "images": [{"index": 0, "url": url}],
                }
                for i, url in enumerate(URLS)
            ]
        }
    variables = {
        QUERY_CONTEXT_KEY: capture_context(plugin, source),
        refs.REFERENCE_KEY: refs.capture_references(plugin, source),
        "launcher_type": source.launcher_type,
        "launcher_id": "201",
        "sender_id": "101",
    }
    replies = []

    class API:
        async def get_query_vars(self):
            return variables.copy()

        async def get_bot_uuid(self):
            return "synthetic-bot"

        async def set_query_var(self, key, value):
            variables[key] = value

        async def reply(self, message):
            replies.append(message)

    api = API()
    listing = list_module.ListEditImageReferences()
    listing.plugin = plugin
    editing = edit_module.EditImage()
    editing.plugin = plugin
    listener = listener_module.ImageContextListener()
    listener.plugin = plugin
    ctx = SimpleNamespace(
        query_id=query_id,
        query_uuid=query_uuid,
        event=SimpleNamespace(
            default_prompt=[Message.model_validate(m) for m in prompts]
        ),
    )
    schemas = []
    for name in ("list_edit_image_references", "edit_image", "generate_image"):
        document = yaml.safe_load(
            (PLUGIN / "components/tools" / f"{name}.yaml").read_text(encoding="utf-8")
        )
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
    tool_names = []
    final = ""
    with (
        patch.object(listener_module, "QueryBasedAPIProxy", return_value=api),
        patch.object(list_module, "QueryBasedAPIProxy", return_value=api),
        patch.object(edit_module, "QueryBasedAPIProxy", return_value=api),
    ):
        await listener.inject_prompt(ctx)
        messages = [
            {"role": m.role, "content": m.content} for m in ctx.event.default_prompt
        ]
        if case == "followup_after_old_single_image_reply":
            messages.extend(
                [
                    {"role": "user", "content": question},
                    {
                        "role": "assistant",
                        "content": "我只能修改一张图，请先把两张拼好再发。",
                    },
                ]
            )
            question = "这两张，按刚才说的做。"
        messages.append({"role": "user", "content": question})
        for _ in range(4):
            response = await client.chat.completions.create(
                model="gpt-6.1-sol",
                messages=messages,
                tools=schemas,
            )
            message = response.choices[0].message
            messages.append(message.model_dump(exclude_none=True))
            if not message.tool_calls:
                final = message.content or ""
                break
            for call in message.tool_calls:
                name = call.function.name
                tool_names.append(name)
                arguments = json.loads(call.function.arguments)
                if name == "list_edit_image_references":
                    result = await listing.call(arguments, None, query_id, query_uuid)
                elif name == "edit_image":
                    result = await editing.call(arguments, None, query_id, query_uuid)
                else:
                    result = "验证失败：此处需要编辑已有图片，不能改成新生成。"
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result}
                )
    selected = []
    if plugin.edit_image.await_count:
        images = plugin.edit_image.await_args.args[1]
        images = images if isinstance(images, list) else [images]
        selected = [image.url for image in images]
    passed = (
        plugin.edit_image.await_count == 1 and selected == URLS and len(replies) == 1
    )
    report = {
        "case": case,
        "passed": passed,
        "tool_names": tool_names,
        "image_backend_calls": plugin.edit_image.await_count,
        "references_in_requested_order": selected == URLS,
        "mock_replies": len(replies),
        "answer": final,
    }
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES)
    args = parser.parse_args()
    api = LocalAPI()
    state = json.loads((STATE / "setup-state.json").read_text(encoding="utf-8"))
    pipeline = api.call("GET", "/api/v1/pipelines/" + state["pipeline_uuid"])[
        "pipeline"
    ]
    config = pipeline["config"]["ai"]["runner_config"][
        "plugin:langbot-team/LocalAgent/default"
    ]
    provider = api.call(
        "GET",
        "/api/v1/provider/providers/" + state["provider_uuid"] + "?include_secret=true",
    )["provider"]
    async with AsyncOpenAI(
        api_key=provider["api_keys"][0],
        base_url=provider["base_url"],
        timeout=80,
        max_retries=0,
        http_client=httpx.AsyncClient(trust_env=False),
    ) as client:
        results = [
            await run_case(client, config["prompt"], case, i)
            for i, case in enumerate(CASES)
            if args.case is None or args.case == case
        ]
    report = {
        "synthetic_only": True,
        "chat_model": "gpt-6.1-sol",
        "image_provider_mocked": True,
        "qq_messages_sent": 0,
        "cases": results,
    }
    filename = (
        f"multi-image-routing-{args.case}-verification.json"
        if args.case
        else "multi-image-routing-verification.json"
    )
    (STATE / filename).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not all(result["passed"] for result in results):
        raise SystemExit("Some routing cases failed; inspect the local report.")


if __name__ == "__main__":
    asyncio.run(main())
