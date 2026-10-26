from types import SimpleNamespace as NS
import json

import pytest

from test_dialogue_replay import replay


@pytest.mark.sdk
@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["free", "direct", "research"])
async def test_replay_followup_uses_its_actual_previous_reply_and_real_host_prompt(route):
    class OfflineProvider:
        stage = ""
        calls = []
        prompts = []

        async def invoke(self, *, messages, **_):
            self.prompts.append((self.stage, [item.content for item in messages]))
            if self.stage == "judge":
                context = json.loads(messages[-1].content)
                text = json.dumps({"action": "reply", "target_message_id": context["reply_candidate_ids"][-1]})
            elif self.stage == "reconcile":
                text = '{"action":"keep"}'
            elif self.stage == "generate":
                text = '{"action":"reply","text":"本次模型实际生成的独特回答"}'
            else:
                text = "本次模型实际生成的独特回答"
            return NS(content=text, tool_calls=None)

    provider = OfflineProvider()
    case = {"id": "test", "route": route, "criteria": "不使用预写的机器人答案", "dialogue": [["a", "最近怎么样"]],
            "followups": [["b", "接着聊"]], "evidence": "合成观察" if route == "research" else ""}
    result = await replay.replay(case, provider, "offline")
    assert all("error" not in turn for turn in result["turns"])
    assert result["turns"][1]["input"][-2]["text"] == "本次模型实际生成的独特回答"
    assert result["turns"][1]["input"][-1]["sender_id"] == "b"
    final = next(prompt for stage, prompt in reversed(provider.prompts) if stage not in {"judge", "reconcile"})
    assert "本次模型实际生成的独特回答" in "\n".join(final)
