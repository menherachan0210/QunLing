"""Replay synthetic conversations through the real Host prompt builders.

No QQ transport or private chat history is used. Runtime mode exercises actual
memory retrieval and the tool loop on synthetic data; external evidence is recorded,
not a live vision/search test. Each follow-up sees that run's actual
previous reply. Run the same script in two source trees with the same provider
for a comparison; retain the JSON and review HTML rather than a keyword score.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, fields
import hashlib
import html
import json
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_cases(path):
    cases = json.loads(path.read_text(encoding="utf-8"))
    names = set()
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get("id"), str) or case["id"] in names
                or case.get("route") not in {"free", "direct", "research", "routing"}
                or not isinstance(case.get("criteria"), str) or not case.get("dialogue")):
            raise ValueError("Invalid or duplicate replay case")
        if case.get("context_source", "prepared_group") not in {"prepared_group", "member_history"}:
            raise ValueError("Invalid replay context source")
        if case.get("context_source") == "member_history" and case["route"] != "direct":
            raise ValueError("Member-only history requires a direct request")
        if type(case.get("mention_bot", False)) is not bool or (
                case.get("mention_bot") and case["route"] not in {"direct", "research"}):
            raise ValueError("Explicit bot mentions require a direct or research replay")
        names.add(case["id"])
        for actor, text in [*case["dialogue"], *case.get("followups", [])]:
            if not isinstance(actor, str) or not isinstance(text, str) or not text:
                raise ValueError("Invalid synthetic conversation turn")
        media = case.get("media_messages", {})
        if (not isinstance(media, dict) or any(
                not isinstance(index, str) or not index.isascii() or not index.isdecimal()
                or str(int(index)) != index or not 1 <= int(index) <= len(case["dialogue"])
                or not isinstance(kind, str) or kind not in {"image", "file", "audio", "video", "forward"}
                for index, kind in media.items())):
            raise ValueError("Invalid attachment source in replay case")
    return cases


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    cards = []
    for item in report["cases"]:
        cards.append("<article><h2>" + html.escape(item["id"]) + "</h2><p>" + html.escape(item["criteria"]) + "</p>"
                     + "".join("<pre>" + html.escape(json.dumps(turn, ensure_ascii=False, indent=2)) + "</pre>"
                               for turn in item["turns"]) + "</article>")
    path.with_suffix(".html").write_text(
        '<!doctype html><meta charset="utf-8"><title>合成群聊回放</title>'
        '<style>body{font:16px/1.6 system-ui;max-width:1000px;margin:32px auto;padding:0 20px}'
        'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f4f7;padding:16px}'
        'article{border-bottom:1px solid #ccc;padding:16px 0}</style><h1>合成群聊回放</h1>'
        '<p>逐项审阅交流目的、成员归属、是否重复、工具需求和表达；不以长度或关键词代替自然度判断。</p>'
        + "".join(cards), encoding="utf-8")


def compare_reports(before, after, path):
    """Blind A/B presentation, no machine-generated naturalness score."""
    a, b = (json.loads(source.read_text(encoding="utf-8")) for source in (before, after))
    left, right = ({case["id"]: case for case in report["cases"]} for report in (a, b))
    if left.keys() != right.keys():
        raise ValueError("Comparison requires the same case set")
    cards, mapping = [], {}
    def transcript(case):
        rows = []
        for index, turn in enumerate(case["turns"]):
            inputs = turn["input"] if index == 0 else turn["input"][-1:]
            rows.extend("<p><b>" + html.escape(m["sender_name"] or m["sender_id"]) + "：</b>"
                        + html.escape(m["text"]) + "</p>" for m in inputs)
            reply = turn.get("reply") or ("[错误：" + turn["error"] + "]" if turn.get("error") else
                "[" + ("重复草稿已抑制" if turn.get("suppressed_draft") else turn.get("action", "无回复")) + "]")
            rows.append("<p class='reply'><b>机器人：</b>" + html.escape(reply) + "</p>")
        return "".join(rows)
    for key in left:
        swapped = bool(hashlib.sha256(key.encode()).digest()[0] % 2)
        pair = (right[key], left[key]) if swapped else (left[key], right[key])
        mapping[key] = {"A": b["label"] if swapped else a["label"], "B": a["label"] if swapped else b["label"]}
        cards.append("<article><h2>" + html.escape(key) + "</h2><p>" + html.escape(left[key]["criteria"])
                     + "</p><div class='pair'>" + "".join("<section><h3>" + name + "</h3>" + transcript(case)
                         + "</section>" for name, case in zip(("A", "B"), pair)) + "</div><details><summary>查看版本对应</summary>"
                     + html.escape(json.dumps(mapping[key], ensure_ascii=False)) + "</details></article>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sources": [str(before), str(after)], "mapping": mapping}, ensure_ascii=False, indent=2), encoding="utf-8")
    path.with_suffix(".html").write_text('<!doctype html><meta charset="utf-8"><title>群聊对话对照</title>'
        '<style>body{font:16px/1.7 system-ui;margin:24px auto;max-width:1300px;padding:0 20px}'
        '.pair{display:grid;grid-template-columns:1fr 1fr;gap:24px}section{background:#f6f7fb;padding:16px;min-width:0}'
        'p{white-space:pre-wrap;overflow-wrap:anywhere}.reply{border-left:3px solid #6964dd;padding-left:12px}'
        'article{border-top:1px solid #ddd;margin-top:32px}@media(max-width:700px){.pair{grid-template-columns:1fr}}</style>'
        '<h1>群聊对话对照</h1><p>A/B 顺序按案例打乱。先看是否理解意图、成员、事实与工具需求，再比较表达；'
        '不把短回复或更多玩笑自动判为更好。合成证据不代表已验证真实识图或 QQ 交付。</p>' + "".join(cards), encoding="utf-8")


async def replay(case, provider, label):
    # Import only for live/source integration runs. --check-only has no SDK or API dependency.
    from qunling.proactive import host
    from qunling.proactive.engine import ConversationContext, Decision, Message, validate_decision
    from langbot_plugin.api.entities.builtin.provider.message import Message as ModelMessage
    from langbot_plugin.api.entities.builtin.platform.message import At, Plain
    from langbot.pkg.api.http.context import ExecutionContext
    from qunling import group_memory
    from qunling.group_context import metadata
    from qunling.knowledge import host as knowledge_host
    from bootstrap.configure import PROMPT

    if hasattr(provider, "begin_case"):
        await provider.begin_case(case)

    bot_id, group_id = "synthetic-bot", "900000001"
    execution = ExecutionContext(instance_uuid="synthetic-instance", workspace_uuid="synthetic-workspace",
                                 bot_uuid=bot_id, placement_generation=1)
    bot = NS(bot_entity=NS(uuid=bot_id, workspace_uuid=execution.workspace_uuid, enable=True),
             enable=True, execution_context=execution, adapter=NS(bot_account_id="10000"))
    bridge = host.HostBridge.__new__(host.HostBridge)
    bridge.bot, bridge._received_at, bridge._request_contexts = bot, {}, {}
    bridge._preference_versions, bridge._preference_subjects = {}, {}
    bridge.store = NS(read_valid=True, read=lambda: {'revision': 'synthetic', 'enabled': True,
        'groups': {group_id: {'mode': mode}}})
    bridge._active = AsyncMock()
    bridge.engine = NS(context_metadata=lambda group, history, participant, reply_to: metadata(
        bot_id, group, history, participant_id=participant, reply_to=reply_to, ignored_terms=("群灵",)))
    bridge.decision_timeout = bridge.reply_timeout = 90
    bridge._model = AsyncMock(return_value=NS(provider=NS(invoke_llm=provider.invoke)))
    bot.adapter._runtime = bot
    calls = []

    async def complete(system, content, *, stage="generate"):
        provider.stage, provider.calls = stage, calls
        return await bridge._complete_raw(system, content, stage=stage)

    bridge._complete = complete
    messages, previous, covered = [], None, []
    settings = getattr(provider, "group_settings", {})
    persona = case.get("persona", settings.get("persona", ""))
    mode = case.get("mode", settings.get("mode", "active"))
    display_name = case.get("display_name", settings.get("display_name", "群灵"))
    samples = case.get("examples", settings.get("examples", []))
    if case.get("context_source") == "member_history":
        # Exercise the actual disabled-public-participation boundary while
        # retaining the administrator's persona and the member's native history.
        mode = "off"
        bridge.store.read = lambda: {"revision": "synthetic", "enabled": True,
            "default_style": settings.get("default_style", ""),
            "default_examples": settings.get("default_examples", []),
            "groups": {group_id: {"mode": mode, "display_name": display_name,
                                  "persona": persona, "examples": samples}}}
    evidence = [{"kind": "image_analysis" if case.get("media") else "text_read",
                 "source": "synthetic-fixture", "text": case["evidence"]}] if case.get("evidence") else []

    def add(actor, text, *, is_bot=False, reply_target=None):
        if not is_bot and hasattr(provider, "actor_id"):
            actor = provider.actor_id(actor)
        key = "s" + str(len(messages) + 1)
        media_kind = case.get("media_messages", {}).get(str(len(messages) + 1))
        attachment = ({"kind": media_kind, "source": "message", "message_id": key,
                       "sender_id": "10000" if is_bot else actor},) if media_kind else ()
        msg = Message(bot_id, group_id, "s" + str(len(messages) + 1), "10000" if is_bot else actor,
                      text, time.time(), sender_name="机器人" if is_bot else "成员" + actor,
                      is_bot=is_bot, has_media=bool(attachment or (case.get("media") and not messages)),
                      reply_to=reply_target.message_id if reply_target else None,
                      reply_recipient_id=reply_target.sender_id if reply_target else None,
                      **({"attachments": attachment} if "attachments" in {field.name for field in fields(Message)} else {}))
        messages.append(msg)
        return msg

    for actor, text in case["dialogue"]:
        add(actor, text, is_bot=actor == "bot")

    result = {"id": case["id"], "criteria": case["criteria"], "label": label, "turns": []}
    with patch.object(group_memory, "recall_group_memory", getattr(provider, "recall_group_memory", AsyncMock(return_value={}))), \
            patch.object(knowledge_host, "recall", AsyncMock(return_value={})), \
            patch.object(host.reply_preferences, "get", side_effect=lambda scope, sender: {
                "sender_id": sender, "mode": "normal", "rule": "", "revision": "0"}), \
            patch.object(host.reply_preferences, "save", side_effect=RuntimeError("Style replay cannot persist member agreements")), \
            patch.object(host, "_shared_knowledge", return_value=[] if hasattr(provider, "runner_reply") else evidence), \
            patch.object(host, "_bridge", return_value=bridge):
        for turn_index in range(1 + len(case.get("followups", []))):
            if turn_index:
                add(*case["followups"][turn_index - 1])
            target = messages[-1]
            attributes = dict(bot_id=bot_id, group_id=group_id, mode=mode, persona=persona,
                messages=tuple(messages), followup_candidate_ids=(target.message_id,) if previous else (),
                last_bot_text=previous.text if previous else "", last_bot_message_id=previous.message_id if previous else None,
                last_bot_at=previous.timestamp if previous else None, display_name=display_name,
                reply_candidate_ids=(target.message_id,), participant_id=target.sender_id,
                focus_message_ids=tuple(m.message_id for m in messages), topic_message_ids=tuple(m.message_id for m in messages),
                covered_message_ids=tuple(covered), examples=samples,
                default_style=settings.get("default_style", ""), default_examples=settings.get("default_examples", []))
            allowed = {field.name for field in fields(ConversationContext)}
            context = ConversationContext(**{k: v for k, v in attributes.items() if k in allowed})
            row = {"input": [asdict(m) for m in messages], "route": case["route"], "calls": calls}
            result["turns"].append(row)
            started = time.monotonic()
            try:
                if case["route"] in {"free", "routing"}:
                    raw = await bridge.judge(context)
                    row["decision"] = raw
                    decision = validate_decision(raw, context)
                    if decision is None:
                        raise ValueError("Invalid routing result")
                    row["action"] = decision.action
                    if decision.action in {"reply", "followup"} and case["route"] != "routing":
                        answer = await bridge.generate(context, decision)
                        if isinstance(answer, Decision):
                            decision = answer
                            row["action"], answer = decision.action, ""
                        elif not answer:
                            row["action"] = "silence"
                        row["reply"] = answer
                        if answer and previous:
                            receipts = [{"receipt_id": previous.message_id, "target_message_id": covered[-1],
                                         "text": previous.text, "timestamp": previous.timestamp}]
                            review = await bridge.reconcile(context, decision, answer, receipts)
                            row["review"] = review
                            if review.get("action") == "covered":
                                row["suppressed_draft"], row["reply"] = answer, ""
                                row["action"] = "silence"
                    if "actions" in case:
                        row["expected_action_pass"] = row["action"] in case["actions"]
                handoff = (hasattr(provider, "runner_reply") and case["route"] == "free"
                           and row.get("action") in {"research", "tool_followup", "task_request"})
                if case["route"] not in {"free", "routing"} or handoff:
                    research = case["route"] == "research" or (handoff and row["action"] != "task_request")
                    state = {"group_id": group_id, "questioner_id": target.sender_id, "participant_id": target.sender_id,
                        "current_message_id": target.message_id, "mode": mode, "free_chat_enabled": True,
                        "actually_mentioned": case.get("mention_bot", False),
                        "display_name": display_name, "persona": persona, "default_style": settings.get("default_style", ""), "examples": samples,
                        "default_examples": settings.get("default_examples", []), "topic_message_ids": list(attributes["topic_message_ids"]),
                        "focus_message_ids": list(attributes["focus_message_ids"]), "covered_message_ids": covered,
                        "continuation_read_only": research, "research_request": research}
                    if handoff and decision.understanding:
                        state["understanding"] = asdict(decision.understanding)
                    if case.get("context_source") != "member_history":
                        bridge.request_context = lambda _: (state, [asdict(m) for m in messages[:-1]])
                    incoming = NS(group=NS(id=group_id), sender=NS(id=target.sender_id, nickname=target.sender_name),
                                  message_id=target.message_id, timestamp=target.timestamp,
                                  message_chain=([At(target=int(bot.adapter.bot_account_id))] if case.get("mention_bot") else [])
                                                + [Plain(text=target.text)])
                    configured = getattr(provider, "runner", {}).get("prompt", [{"role": "system", "content": PROMPT}])
                    query = NS(adapter=bot.adapter, message_event=incoming, _execution_context=execution,
                               prompt=NS(messages=[ModelMessage(**item) for item in configured]))
                    if hasattr(provider, "restore_query"):
                        await provider.restore_query(query=query, target=target, case=case)
                    if hasattr(provider, "runner_reply"):
                        from qunling.dialogue_focus import prepare
                        await prepare(query)
                    if hasattr(provider, "prepare_query"):
                        await provider.prepare_query(query=query, target=target, case=case)
                    host.prepare_prompt(query)
                    if not any(item.content.startswith(host.PROMPT_MARKER) for item in query.prompt.messages):
                        raise ValueError("Host prompt was not prepared")
                    # Represents the real event after Host projection, not a new instruction inferred from tool output.
                    provider.stage, provider.calls = "tool_reply" if evidence else "direct_reply", calls
                    if hasattr(provider, "runner_reply"):
                        response = await provider.runner_reply(query=query, target=target, case=case)
                    else:
                        # Lightweight style smoke test; full runtime mode below
                        # uses LocalAgent assembly, history, compaction and loop.
                        response = await provider.invoke(messages=query.prompt.messages)
                    row["reply"], row["action"] = response.content, "reply"
                if row.get("reply"):
                    # The real adapter/engine records both the source message
                    # and its sender after a confirmed reply. Dropping this
                    # provenance makes a later member's "what about me?" look
                    # like a correction of an answer meant for someone else.
                    delivered_target = (next(m for m in messages if m.message_id == decision.target_message_id)
                                        if case["route"] == "free" and not handoff else target)
                    covered.append(delivered_target.message_id)
                    previous = add("bot", row["reply"], is_bot=True, reply_target=delivered_target)
            except Exception as exc:
                # Transport error strings can contain URLs/credentials. Store only a fixed type name.
                row["error"] = type(exc).__name__
            row["elapsed_seconds"] = round(time.monotonic() - started, 3)
            row["calls"] = list(calls)
            calls.clear()
    return result


class Provider:
    def __init__(self, client, model, max_calls):
        self.client, self.model, self.max_calls = client, model, max_calls
        self.count, self.stage, self.calls = 0, "", []

    async def invoke(self, *, messages, **_):
        if self.count >= self.max_calls:
            raise RuntimeError("Replay call budget exhausted")
        self.count += 1
        wire = [item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else item for item in messages]
        # ModelMessage carries SDK-only fields; only the two text-message fields are sent.
        wire = [{"role": item["role"], "content": item["content"]} for item in wire]
        raw = json.dumps(wire, ensure_ascii=False)
        observation = {"stage": self.stage, "prompt_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                       "input_characters": len(raw)}
        self.calls.append(observation)
        started = time.monotonic()
        try:
            response = await self.client.chat.completions.create(model=self.model, messages=wire,
                                                                max_completion_tokens=4096)
            observation["usage"] = response.usage.model_dump() if response.usage else None
            text = response.choices[0].message.content
            if not text:
                raise ValueError("No text response")
            # This harness sends synthetic messages only. Preserve the actual
            # model draft so protocol failures can be inspected, not retried away.
            observation["response_text"] = text
            return NS(content=text, tool_calls=None)
        finally:
            observation["seconds"] = round(time.monotonic() - started, 3)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "tests/fixtures/conversation_style_cases.json")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--provider-config", type=Path, help="Existing protected AI config; never copied into reports")
    parser.add_argument("--runtime-db", type=Path, help="Read-only Host SQLite DB; reuse active model and pipeline settings")
    parser.add_argument("--pipeline", help="Pipeline UUID when the Host has more than one")
    parser.add_argument("--local-agent", type=Path, help="Packaged/patched LocalAgent source for runtime replay")
    parser.add_argument("--group-settings", type=Path, help="Read-only persona/mode/examples projection for the group under evaluation")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--label", default="current")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--max-calls", type=int, default=100)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"), help="Offline A/B review; no API requests")
    args = parser.parse_args()
    if args.output and (args.output.exists() or args.output.with_suffix(".html").exists()):
        parser.error("Choose a new output path; existing evidence is never overwritten")
    if args.compare:
        if not args.output:
            parser.error("--compare requires --output")
        compare_reports(*args.compare, args.output)
        return
    cases = load_cases(args.cases)
    if args.case:
        unknown = set(args.case) - {case["id"] for case in cases}
        if unknown:
            parser.error("Unknown case")
        cases = [case for case in cases if case["id"] in args.case]
    if args.check_only:
        print(json.dumps({"cases": len(cases), "turns": sum(1 + len(c.get("followups", [])) for c in cases),
                          "api_requests": 0, "qq_messages_sent": 0}))
        return
    if args.runtime_db:
        if not args.local_agent or not args.output or not 1 <= args.max_calls <= 500 or args.provider_config:
            parser.error("Runtime replay needs --runtime-db, --local-agent, --output and a call budget; omit --provider-config")
        from scripts.dialogue_runtime import RuntimeProvider
        provider = RuntimeProvider(args.runtime_db, args.local_agent, args.output.with_suffix(".state"), args.max_calls, args.pipeline)
        if args.group_settings:
            provider.group_settings = json.loads(args.group_settings.read_text(encoding="utf-8"))
        report = {"label": args.label, "assembly_mode": "production_model_and_runner", "cases": [],
            "models": {role: provider.model_rows[key]["name"] for role, key in provider.roles.items()},
            "limits": "Isolated synthetic replay; actual Host model settings, LocalAgent history/compaction/tool loop and LTM embedding/retrieval/injection. External tool observations are recorded fixtures; unrecorded tools return unavailable. No QQ dispatch or live image perception.",
            "api_requests": 0, "qq_messages_sent": 0}
        try:
            for case in cases:
                report["cases"].append(await replay(case, provider, args.label))
                report["api_requests"] = provider.count
                report["assembly"] = provider.assembly_reports
                report["tool_calls"] = provider.tool_calls
                write_report(args.output, report)
                print(json.dumps({"case": case["id"], "requests": provider.count}), flush=True)
        finally:
            provider.close()
        report["errors"] = sum("error" in turn for case in report["cases"] for turn in case["turns"])
        write_report(args.output, report)
        if report["errors"]:
            raise SystemExit(1)
        return
    if not args.provider_config or not args.output or not 1 <= args.max_calls <= 500:
        parser.error("Live replay needs --provider-config, --output and a call budget of 1–500")
    from openai import AsyncOpenAI
    import httpx
    credentials = json.loads(args.provider_config.read_text(encoding="utf-8"))
    report = {"label": args.label, "model": credentials["chat_model"], "cases": [],
              "limits": "Synthetic text/evidence; no real vision, network search, QQ send or production latency", "api_requests": 0}
    report["source_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
        ("qunling/proactive/host.py", "qunling/proactive/engine.py", "qunling/group_persona.py", "config/chat-style.txt")}
    async with AsyncOpenAI(api_key=credentials["api_key"], base_url=credentials["base_url"],
                          max_retries=0, timeout=90, http_client=httpx.AsyncClient(trust_env=False)) as client:
        provider = Provider(client, credentials["chat_model"], args.max_calls)
        # Sequential cases keep global mocked evidence and provider stage attribution isolated.
        for case in cases:
            report["cases"].append(await replay(case, provider, args.label))
            report["api_requests"] = provider.count
            write_report(args.output, report)
            print(json.dumps({"case": case["id"], "requests": provider.count}), flush=True)
    durations = [turn["elapsed_seconds"] for case in report["cases"] for turn in case["turns"]]
    report["median_turn_seconds"] = round(statistics.median(durations), 3)
    report["errors"] = sum("error" in turn for case in report["cases"] for turn in case["turns"])
    write_report(args.output, report)
    print(json.dumps({key: report[key] for key in ("api_requests", "errors", "median_turn_seconds")}))
    if report["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
