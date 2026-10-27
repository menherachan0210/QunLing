"""Exercise final runner assembly with a fake model transport, never a QQ send."""
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

pytestmark = pytest.mark.sdk
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from scripts.dialogue_runtime import RuntimeProvider
    source = ROOT / ".local/conversation-agent"
    if not source.is_dir():
        packages = list(Path("/opt/qq-ai-bot/packages").glob("*LocalAgent*.lbpkg"))
        if not packages:
            pytest.skip("Requires the built LocalAgent package (the Linux image supplies it)")
        source = tmp_path / "agent"
        with zipfile.ZipFile(packages[0]) as archive:
            archive.extractall(source)
    database = tmp_path / "config.sqlite"
    with sqlite3.connect(database) as db:
        db.executescript("""CREATE TABLE legacy_pipelines(uuid,workspace_uuid,config);
            CREATE TABLE llm_models(uuid,workspace_uuid,name,provider_uuid,abilities,reasoning_config,extra_args);
            CREATE TABLE model_providers(uuid,workspace_uuid,name,requester,base_url,api_keys);""")
        db.execute("INSERT INTO legacy_pipelines VALUES (?,?,?)", ("pipeline", "workspace", json.dumps({"ai": {
            "runner": {"id": "plugin:langbot-team/LocalAgent/default"}, "runner_config": {
                "plugin:langbot-team/LocalAgent/default": {"model": {"primary": "model", "reasoning": {}},
                    "box-enabled": False, "prompt": []}}}})))
        db.execute("INSERT INTO llm_models VALUES (?,?,?,?,?,?,?)", ("model", "workspace", "gpt-4.1", "provider",
            '["func_call"]', '{"level":"provider_default"}', '{"temperature":0.8}'))
        db.execute("INSERT INTO model_providers VALUES (?,?,?,?,?,?)", ("provider", "workspace", "test",
            "openai-chat-completions", "https://example.test/v1", '["test-key"]'))
    provider = RuntimeProvider(database, source, tmp_path / "state", 30)
    yield provider
    provider.close()


@pytest.mark.asyncio
async def test_runtime_tool_pair_stays_in_its_member_lane(runtime, monkeypatch):
    import litellm
    from langbot.pkg.provider.modelmgr.requesters import litellmchat
    from test_dialogue_replay import replay
    calls = []
    async def completion(**kwargs):
        if "[QunLing 本轮理解]" in str(kwargs["messages"]):
            return litellm.ModelResponse(model="gpt-4.1", choices=[{"message": {"role": "assistant",
                "content": '{"purpose":"继续讨论比赛","context_message_ids":[],"information_needs":["history"]}'}}])
        calls.append(kwargs)
        if len(calls) == 1:
            return litellm.ModelResponse(model="gpt-4.1", choices=[{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{"id": "lookup", "type": "function",
                    "function": {"name": "search_web", "arguments": '{"query":"测试比赛"}'}}]}}])
        return litellm.ModelResponse(model="gpt-4.1", choices=[{"message": {"role": "assistant", "content": "只打完首轮。"}}])
    monkeypatch.setattr(litellmchat, "acompletion", completion)
    case = {"id": "tool-pair", "route": "research", "dialogue": [["a", "查一下比赛"]],
        "followups": [["a", "那还没结束"], ["b", "我也在看这场比赛"]],
        "criteria": "完整工具对与成员会话隔离", "tools": ["search_web"],
        "evidence": "只打完首轮"}
    result = await replay.replay(case, runtime, "offline")
    assert all("error" not in turn for turn in result["turns"]), result
    assert runtime.tool_calls == [{"case": "tool-pair", "tool": "search_web", "parameters": {"query": "测试比赛"}, "error": False}]
    assert runtime.assembly_reports[1]["history_count"] == 4
    assert runtime.assembly_reports[2]["history_count"] == 0
    assert not any(m["role"] == "tool" for m in runtime.assembly_reports[2]["messages"])
    messages = runtime.assembly_reports[1]["messages"]
    assert len([m for m in messages if m["role"] == "tool"]) == 1
    assert calls[0]["temperature"] == .8
    assert sum("那还没结束" in str(m.get("content")) for m in messages) == 1


@pytest.mark.asyncio
async def test_runtime_compaction_persists_conversation_summary(runtime, monkeypatch):
    import litellm
    from langbot.pkg.provider.modelmgr.requesters import litellmchat
    from test_dialogue_replay import replay
    calls = []
    async def completion(**kwargs):
        if "[QunLing 本轮理解]" in str(kwargs["messages"]):
            return litellm.ModelResponse(model="gpt-4.1", choices=[{"message": {"role": "assistant",
                "content": '{"purpose":"回看前情后继续聊猫","context_message_ids":[],"information_needs":["history"]}'}}])
        calls.append(kwargs)
        summary = "概括已发生的对话" in str(kwargs["messages"])
        answer = "群友之前聊完了工具任务，现在只是聊猫；无待办。" if summary else "猫已经躺平了。"
        return litellm.ModelResponse(model="gpt-4.1", choices=[{"message": {"role": "assistant", "content": answer}}])
    monkeypatch.setattr(litellmchat, "acompletion", completion)
    case = {"id": "compact", "route": "direct", "dialogue": [["a", "今天猫又睡了一天"]],
        "followups": [["a", "任务做完了，接着聊猫"]], "criteria": "旧任务不会强制变成待办", "tools": [],
        "history": [["a" if i % 2 == 0 else "bot", "之前做完了文件任务，后来开始聊猫。" * 150] for i in range(20)],
        "runner_overrides": {"context-window-tokens": 9000, "context-reserve-tokens": 1000,
                             "context-keep-recent-tokens": 1000, "context-summary-tokens": 500}}
    result = await replay.replay(case, runtime, "offline")
    assert all("error" not in turn for turn in result["turns"]), result
    assert runtime.assembly_reports[0]["checkpoint_present"]
    assert runtime.assembly_reports[1]["checkpoint_present"]
    assert any("概括已发生的对话" in str(c["messages"]) for c in calls), [str(c["messages"])[:300] for c in calls]
    messages = runtime.assembly_reports[1]["messages"]
    summaries = [m for m in messages if "<conversation_summary>" in str(m.get("content"))]
    assert summaries and all(m["role"] == "user" for m in summaries)
    assert "Goal / Progress / Next Steps" not in str(calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("context_source", ["prepared_group", "member_history"])
@pytest.mark.parametrize("mention_bot", [False, True])
async def test_final_review_is_the_reply_and_the_followup_history(runtime, monkeypatch, context_source, mention_bot):
    import litellm
    from langbot.pkg.provider.modelmgr.requesters import litellmchat
    from test_dialogue_replay import replay

    async def completion(**kwargs):
        system = kwargs["messages"][0]["content"]
        if "你核对一条即将发到群里的回复" in system:
            answer = '{"action":"replace","reason":"原文重复复盘错误","text":"接着聊新话题。"}'
        elif "[QunLing 本轮理解]" in system:
            answer = '{"purpose":"继续群聊","context_message_ids":[],"information_needs":["history"]}'
        else:
            answer = "原始草稿：又解释了一遍旧错误。"
        return litellm.ModelResponse(model="gpt-4.1", choices=[{"message": {"role": "assistant", "content": answer}}])

    monkeypatch.setattr(litellmchat, "acompletion", completion)
    case = {"id": "review-history", "route": "direct", "context_source": context_source, "mention_bot": mention_bot,
        "dialogue": [["a", "换个话题"]],
        "followups": [["a", "接着说"]], "criteria": "历史必须保存实际发出的回复", "tools": []}
    result = await replay.replay(case, runtime, "offline")
    assert [turn.get("reply") for turn in result["turns"]] == ["接着聊新话题。"] * 2, result
    history = str(runtime.assembly_reports[1]["messages"])
    assert "接着聊新话题。" in history and "原始草稿：" not in history
    assert '"actually_mentioned": ' + str(mention_bot).lower() in history
    persisted = [json.loads(row[0]) for row in runtime.history.execute("SELECT item FROM turns ORDER BY seq")]
    assert [item["content_json"]["content"] for item in persisted if item["role"] == "assistant"] == ["接着聊新话题。"] * 2
    # Host understanding and final review must see the same canonical history
    # as LocalAgent, including when spontaneous group participation is off.
    second = result["turns"][1]
    judge = next(call for call in second["calls"] if call["stage"] == "judge")
    assert "接着聊新话题。" in str(judge["messages"])
    review = next(call for call in second["calls"] if call["stage"] == "review")
    evidence = json.loads(review["messages"][-1]["content"])["dialogue"]
    assert "接着聊新话题。" in str(evidence["native_history"])
    if context_source == "member_history":
        assert [message["message_id"] for message in evidence["messages"]] == ["s3"]


@pytest.mark.asyncio
async def test_real_memory_listener_records_the_injected_profile(runtime, monkeypatch, caplog):
    import litellm
    from langbot.pkg.provider.modelmgr.requesters import litellmchat
    from test_dialogue_replay import replay

    async def completion(**kwargs):
        system = kwargs["messages"][0]["content"]
        answer = ('{"action":"keep","reason":"无实质问题"}'
                  if "你核对一条即将发到群里的回复" in system else "你喝咖啡不加糖。")
        return litellm.ModelResponse(model="gpt-4.1", choices=[{"message": {"role": "assistant", "content": answer}}])

    monkeypatch.setattr(litellmchat, "acompletion", completion)
    case = {"id": "memory-injection-audit", "route": "direct", "dialogue": [["a", "我喝咖啡加糖吗？"]],
        "criteria": "真实记忆监听器写入注入审计，复核也收到同一画像", "tools": [],
        "profiles": {"a": {"name": "小周", "preferences": ["喝咖啡不加糖"], "traits": [], "notes": ""}}}
    result = await replay.replay(case, runtime, "offline")
    assert "error" not in result["turns"][0], result
    snapshot = await runtime.memory.memory_store.get_injection_snapshot("synthetic-bot:group_900000001")
    assert snapshot["injected"] is True and snapshot["injection_chars"] > 0
    assert snapshot["speaker"]["id"] == runtime.actor_id("a")
    assert snapshot["speaker_profile"]["name"] == "小周"
    assert "failed to persist injection snapshot" not in caplog.text
    review = next(c for c in result["turns"][0]["calls"] if c["stage"] == "review")
    assert "喝咖啡不加糖" in str(review["messages"])
