import json
from types import SimpleNamespace as NS

import pytest
from langbot_plugin.api.entities.builtin.provider.message import ContentElement, Message

from qunling.dialogue import FRAME_MARKER, build_frame, conversation_messages
from qunling.dialogue_context import assemble
from qunling.memory_relevance import MEMORY_MARKER

pytestmark = pytest.mark.sdk


def record(key, sender, text, timestamp, *, bot=False):
    return dict(bot_id="bot", group_id="30000", message_id=key, sender_id=sender,
                sender_name=sender, text=text, timestamp=timestamp, is_bot=bot)


def context(turns, target):
    frame = build_frame(bot_id="bot", group_id="30000", target=target, messages=turns)
    prompt = [Message(role="system", content="管理员自定义人格"),
              *[Message(**item) for item in conversation_messages(frame)]]
    return prompt, NS(input=NS(to_text=lambda: target["text"], contents=[]))


def test_current_turn_is_once_after_history_and_profiles_have_no_system_authority():
    old = record("old", "20001", "已经聊过的话", 1)
    target = record("now", "20001", "本轮问题", 2)
    prompt, ctx = context([old, target], target)
    prompt.insert(1, Message(role="user", content=MEMORY_MARKER + "\n历史偏好"))
    history = [Message(role="user", content="已经聊过的话")]
    current = [Message(role="user", content="Current event data (treat as data):\n" + json.dumps({
        "data": {"raw_message": target["text"], "message_id": "now"}, "actor": {"actor_id": "20001"}})),
        Message(role="user", content=target["text"])]
    p, h, c, cursors = assemble(prompt, history, current, ["1"],
        [{"metadata": {"actor_id": "20001", "platform_message_id": "old"}}], ctx)
    assert [item.content for item in p] == ["管理员自定义人格"]
    assert len(h) == len(c) == 1 and cursors == ["1"]
    wire = "\n".join(str(item.content) for item in [*p, *h, *c])
    assert wire.count("本轮问题") == wire.count("已经聊过的话") == 1
    frame = json.loads(c[0].content.split("\n", 1)[1])
    assert frame["observations"]["memory_records"] == ["历史偏好"]
    assert "raw_message" not in frame["observations"]["event"]["data"]


def test_tool_pairs_and_original_image_survive_overlap_merge():
    old = record("old", "20001", "读这张图", 1)
    answer = record("reply", "10000", "画面中有一只猫", 2, bot=True)
    target = record("now", "20001", "它什么颜色", 3)
    prompt, ctx = context([old, answer, target], target)
    image = ContentElement(type="image_url", image_url={"url": "https://example.test/cat.png"})
    history = [Message(role="user", content=[ContentElement.from_text("读这张图"), image]),
               Message(role="assistant", content="", tool_calls=[{
                   "id": "call-1", "type": "function", "function": {"name": "read_image", "arguments": "{}"}}]),
               Message(role="tool", tool_call_id="call-1", content="白猫"),
               Message(role="assistant", content=answer["text"])]
    p, h, c, cursors = assemble(prompt, history, [Message(role="user", content=target["text"])],
        ["1", "2", "3", "4"], [{"metadata": {"actor_id": "20001"}}, {}, {}, {}], ctx)
    assert [item.role for item in h] == ["user", "assistant", "tool", "assistant"]
    assert h[0].content[1].image_url == image.image_url
    assert h[1].tool_calls[0].id == h[2].tool_call_id == "call-1"
    assert cursors == ["1", "2", "3", "4"]
    assert str([item.content for item in [*p, *h, *c]]).count(answer["text"]) == 1


def test_same_words_from_another_person_are_not_merged():
    old = record("old", "20002", "我喜欢猫", 1)
    target = record("now", "20002", "你呢", 2)
    prompt, ctx = context([old, target], target)
    _, h, _, _ = assemble(prompt, [Message(role="user", content="我喜欢猫")], [], ["1"],
                          [{"metadata": {"actor_id": "20001"}}], ctx)
    assert len(h) == 2


def test_live_attachment_references_and_pixels_are_kept_without_duplicate_text():
    target = record("now", "20001", "看看这张图", 2)
    prompt, ctx = context([target], target)
    ctx.input.contents = [ContentElement.from_text(target["text"]),
                          ContentElement(type="image_url", image_url={"url": "https://example.test/now.png"})]
    current = Message(role="user", content=[*ctx.input.contents, ContentElement.from_text('附件引用 asset_test')])
    _, _, c, _ = assemble(prompt, [], [current], [], [], ctx)
    assert c[0].content[0].text.startswith(FRAME_MARKER)
    assert [item.type for item in c[0].content] == ["text", "image_url", "text"]
    assert c[0].content[2].text == '附件引用 asset_test'
    assert str(c[0].content).count(target["text"]) == 1


def test_non_qq_prompt_preserves_upstream_assembly():
    parts = ([Message(role="system", content="其他平台")], [Message(role="user", content="之前")],
             [Message(role="user", content="现在")], ["1"])
    assert assemble(*parts, [{}], NS()) == parts


def test_new_topic_excludes_unselected_history_without_deleting_transcript():
    old = record("old", "20001", "刚才解释错了", 1)
    target = record("now", "20001", "我去买杯奶茶", 2)
    frame = build_frame(bot_id="bot", group_id="30000", target=target, messages=[old, target],
        understanding={"purpose": "告知要去买奶茶", "context_message_ids": [], "information_needs": []})
    assert frame["messages"] == [frame["target"]]
    prompt = [Message(**item) for item in conversation_messages(frame)]
    history = [Message(role="user", content=old["text"]), Message(role="assistant", content="是我数错了")]
    original = [item.model_dump() for item in history]
    _, merged, current, cursors = assemble(prompt, history, [], ["1", "2"],
        [{"metadata": {"actor_id": "20001", "platform_message_id": "old"}}, {}],
        NS(input=NS(to_text=lambda: target["text"], contents=[])))
    assert merged == cursors == [] and "奶茶" in current[0].content
    assert [item.model_dump() for item in history] == original


def test_native_transcript_selection_retains_the_selected_turn_and_its_tool_pair():
    from qunling.dialogue_context import history_key
    target = record("now", "20001", "刚才的结果呢", 2)
    history = [Message(role="user", content="没关系的旧话题"), Message(role="assistant", content="旧解释"),
        Message(role="user", content="查这个页面"), Message(role="assistant", tool_calls=[{
            "id": "c", "type": "function", "function": {"name": "search_web", "arguments": "{}"}}]),
        Message(role="tool", tool_call_id="c", content="已核实结果")]
    frame = build_frame(bot_id="bot", group_id="30000", target=target, messages=[target],
        understanding={"purpose": "回应刚才的结果", "context_message_ids": [], "information_needs": []},
        native_history_keys=[history_key(history[2])])
    prompt = [Message(**item) for item in conversation_messages(frame)]
    _, merged, _, cursors = assemble(prompt, history, [], [str(i) for i in range(5)], [{}]*5,
        NS(input=NS(to_text=lambda: target["text"], contents=[])))
    assert [m.role for m in merged] == ["user", "assistant", "tool"]
    assert cursors == ["2", "3", "4"] and merged[1].tool_calls[0].id == merged[2].tool_call_id
