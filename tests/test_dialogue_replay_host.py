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
                target = context["reply_candidate_ids"][-1]
                text = json.dumps({"action": "reply", "target_message_id": target,
                    "understanding": {"purpose": "接续问候", "context_message_ids": [m["message_id"] for m in context["messages"][-3:]], "information_needs": [],
                        "participation": {"conversation": "群友在邀请机器人聊天", "role": "addressed",
                                          "entry_point": "回应问候", "context_sufficient": True}}})
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


@pytest.mark.sdk
@pytest.mark.asyncio
async def test_replay_counts_generator_silence_as_the_final_action():
    class Provider:
        stage = ""
        calls = []

        async def invoke(self, *, messages, **_):
            if self.stage == "judge":
                context = json.loads(messages[-1].content)
                target = context["reply_candidate_ids"][-1]
                text = json.dumps({"action": "reply", "target_message_id": target,
                    "understanding": {"purpose": "接续聊天", "context_message_ids": [target], "information_needs": [],
                        "participation": {"conversation": "群友闲聊", "role": "joining",
                            "entry_point": "接续原话中的反差", "context_sufficient": True}}})
            else:
                text = '{"action":"silence"}'
            return NS(content=text, tool_calls=None)

    case = {"id": "silence", "route": "free", "dialogue": [["a", "又整上了"]],
            "criteria": "生成阶段可以推翻前置判断", "actions": ["silence"]}
    row = (await replay.replay(case, Provider(), "offline"))["turns"][0]
    assert "error" not in row and row["action"] == "silence"
    assert row["expected_action_pass"] and not row["reply"]
