"""Isolated replay using production Host requesters and the packaged LocalAgent.

Reads model/pipeline settings, never starts a bot or writes a production DB.
Replay history and compaction checkpoints live in their own SQLite database.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace as NS


def load_configuration(database, pipeline_id=None):
    with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = list(db.execute("SELECT uuid,workspace_uuid,config FROM legacy_pipelines"))
        rows = [row for row in rows if not pipeline_id or row["uuid"] == pipeline_id]
        if len(rows) != 1:
            raise ValueError("Select exactly one pipeline for runtime replay")
        pipeline = dict(rows[0])
        pipeline["config"] = json.loads(pipeline["config"])
        models, providers = {}, {}
        for table, result in (("llm_models", models), ("model_providers", providers)):
            for row in db.execute(f"SELECT * FROM {table} WHERE workspace_uuid=?", (pipeline["workspace_uuid"],)):
                item = dict(row)
                for field in ("extra_args", "abilities", "reasoning_config", "api_keys"):
                    if field in item and isinstance(item[field], str):
                        item[field] = json.loads(item[field])
                result[item["uuid"]] = item
    return pipeline, models, providers


class RuntimeProvider:
    """Calls the actual runtime provider, including final argument translation."""
    def __init__(self, database, local_agent, output_dir, max_calls, pipeline_id=None):
        from langbot.pkg.agent.runner.config_resolver import RunnerConfigResolver
        from langbot.pkg.api.http.context import ExecutionContext
        from langbot.pkg.provider.tools.toolmgr import ToolManager

        local_agent = Path(local_agent).resolve()
        if not (local_agent / "pkg/context_pipeline.py").is_file():
            raise ValueError("A built/patched LocalAgent source directory is required")
        sys.path.insert(0, str(local_agent))
        self.pipeline, self.model_rows, self.provider_rows = load_configuration(database, pipeline_id)
        config = self.pipeline["config"]
        self.runner = copy.deepcopy(RunnerConfigResolver.resolve_runner_config(config,
            RunnerConfigResolver.resolve_runner_id(config)))
        primary = self.runner["model"]["primary"]
        self.roles = {"chat": primary, "judge": config.get("ai", {}).get("qunling_decision_model") or primary,
                      "reconcile": config.get("ai", {}).get("qunling_reconcile_model") or primary,
                      "review": config.get("ai", {}).get("qunling_reconcile_model") or primary}
        self.execution = ExecutionContext(instance_uuid="isolated-replay", workspace_uuid=self.pipeline["workspace_uuid"],
                                          bot_uuid="synthetic-bot", placement_generation=1)
        self.ap = NS(logger=logging.getLogger("qunling.replay"))
        self.ap.tool_mgr = ToolManager(self.ap)
        self.models = {}
        self.count, self.max_calls, self.stage, self.calls = 0, max_calls, "", []
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.history = sqlite3.connect(self.output_dir / "replay-history.sqlite3")
        self.history.execute("CREATE TABLE IF NOT EXISTS turns (seq INTEGER PRIMARY KEY, conversation TEXT, item TEXT)")
        self.history.execute("CREATE TABLE IF NOT EXISTS state (conversation TEXT, key TEXT, value TEXT, PRIMARY KEY(conversation,key))")
        self.tool_calls = []
        self.assembly_reports = []
        self.memory = None

    @staticmethod
    def actor_id(actor):
        return actor if str(actor).isdigit() else str(200000000 + int(hashlib.sha256(actor.encode()).hexdigest()[:6], 16))

    async def begin_case(self, case):
        self.memory = ReplayMemory(self, case) if case.get("memories") or case.get("profiles") else None
        if self.memory:
            await self.memory.seed()

    async def recall_group_memory(self, bot, group_id, participant_id, text, *, mode="all"):
        from qunling.group_memory import _records
        if self.memory is None:
            return {}
        entries = await self.memory.retrieve(text, participant_id, profiles=mode != "episodes", episodes=mode != "profiles")
        return {"group_id": group_id, "participant_id": participant_id,
                "records": _records(entries, "synthetic-bot", group_id)}

    def model(self, model_id=None, reasoning_level=None):
        from langbot.pkg.entity.persistence.model import LLMModel, ModelProvider
        from langbot.pkg.provider.modelmgr.requester import RuntimeLLMModel, RuntimeProvider as HostProvider
        from langbot.pkg.provider.modelmgr.requesters import litellmchat
        from langbot.pkg.provider.modelmgr.token import TokenManager
        from langbot.pkg.provider.modelmgr.reasoning import model_with_reasoning_level
        import yaml

        model_id = model_id or self.roles.get(self.stage, self.roles["chat"])
        if model_id not in self.models:
            row = self.model_rows[model_id]
            provider_row = self.provider_rows[row["provider_uuid"]]
            manifest = None
            for path in Path(litellmchat.__file__).parent.glob("*.yaml"):
                value = yaml.safe_load(path.read_text(encoding="utf-8"))
                if value.get("metadata", {}).get("name") == provider_row["requester"]:
                    manifest = value
                    break
            provider_name = (manifest or {}).get("spec", {}).get("litellm_provider")
            if not provider_name:
                raise ValueError("Runtime replay currently requires a Host LiteLLM requester")
            requester = litellmchat.LiteLLMRequester(self.ap, {"base_url": provider_row["base_url"],
                "requester_name": provider_row["requester"], "custom_llm_provider": provider_name})
            entity = ModelProvider(**provider_row)
            provider = HostProvider(self.execution, entity, TokenManager(entity.uuid, entity.api_keys or []), requester)
            self.models[model_id] = RuntimeLLMModel(self.execution, LLMModel(**row), provider)
        model = self.models[model_id]
        level = reasoning_level if reasoning_level is not None else self.runner.get("model", {}).get("reasoning", {}).get(model_id)
        return model_with_reasoning_level(model, level)

    async def invoke(self, *, messages, model_id=None, reasoning_level=None, funcs=None,
                     extra_args=None, remove_think=True, **_):
        if self.count >= self.max_calls:
            raise RuntimeError("Replay model request budget exhausted")
        self.count += 1
        model = self.model(model_id, reasoning_level)
        args = await model.provider.requester._build_completion_args(model, messages, funcs or [], extra_args or {})
        # Allowlist the effective settings: no API keys, headers, URLs or hidden
        # provider fields are copied into an evaluation result.
        settings = {key: args[key] for key in ("model", "temperature", "top_p", "max_tokens",
            "max_completion_tokens", "reasoning_effort", "thinking", "tool_choice") if key in args}
        wire = args["messages"]
        raw = json.dumps(wire, ensure_ascii=False, default=str)
        observation = {"stage": self.stage, "settings": settings, "input_characters": len(raw),
                       "prompt_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                       "messages": wire, "tools": [item["function"]["name"] for item in args.get("tools") or []]}
        self.calls.append(observation)
        started = time.monotonic()
        try:
            response = await model.provider.invoke_llm(query=None, model=model, messages=messages, funcs=funcs or [],
                extra_args=extra_args or {}, remove_think=remove_think, execution_context=self.execution)
            observation["response_text"] = response.content
            return response
        finally:
            observation["seconds"] = round(time.monotonic() - started, 3)

    async def restore_query(self, *, query, target, case):
        # Host preprocessing projects this member's canonical transcript before
        # interpreting the current turn. LocalAgent reads the same history later.
        from langbot.pkg.agent.runner.transcript_store import TranscriptStore
        api = ReplayAPI(self, case["id"] + ":" + target.sender_id, query, target, case)
        view = TranscriptStore(None)
        query.messages = [message for item in api.items[-view.HARD_LIMIT:]
                          if (message := view._transcript_item_to_provider_message(
                              {**item, "item_type": "message"})) is not None]
        query._replay_prepared_api = api

    async def prepare_query(self, *, query, target, case):
        # Production memory listeners run after understanding and before the
        # final Host prompt is frozen. Preserve that order in isolated replay.
        if self.memory is not None:
            api = query._replay_prepared_api
            await self.memory.inject(api)
            query._replay_prepared_api = api

    async def runner_reply(self, *, query, target, case):
        from langbot_plugin.api.entities.builtin.provider.message import ContentElement
        from pkg.agent_core import AgentLoop, LangBotModelAdapter
        from pkg.config import model_reasoning_levels
        from pkg.run_assembly import AgentRunAssembler

        # Match QunLing's QQ member lanes. Public group snapshots are shared,
        # while runner transcripts, tool results and checkpoints are per member.
        conversation = case["id"] + ":" + target.sender_id
        api = getattr(query, "_replay_prepared_api", None) or ReplayAPI(self, conversation, query, target, case)
        config = copy.deepcopy(self.runner)
        # Case overrides are reported. Production defaults remain the baseline;
        # a small window allows an explicit compaction regression scenario.
        config.update(case.get("runner_overrides", {}))
        if case.get("memories") or case.get("profiles"):
            config["knowledge-bases"] = ["replay-memory"]
            from qunling.memory_recall import deduplicate, RUNNER
            envelope = {"config": config}
            deduplicate(api.query_scope, envelope, RUNNER)
            config = envelope["config"]
        ctx = NS(config=config, input=NS(to_text=lambda: target.text,
            contents=[ContentElement.from_text(target.text)], attachments=[]),
            event=NS(event_id=target.message_id, event_type="message.received", source="isolated-replay",
                     data={"message_id": target.message_id}), actor=None, subject=None,
            context=NS(conversation_id=conversation, available_apis=NS(history_page=True, state=True, prompt_get=True)),
            resources=NS(tools=api.tools, skills=[]), runtime=NS(metadata={"streaming_supported": False}),
            delivery=NS(supports_streaming=False))
        assembly = await AgentRunAssembler(api, ctx).assemble()
        self.assembly_reports.append({"case": case["id"], "target": target.message_id,
            "history_count": len(api.items), "messages": [item.model_dump(exclude_none=True) for item in assembly.messages],
            "tool_names": [tool.name for tool in assembly.tools], "overrides": case.get("runner_overrides", {}),
            "checkpoint_present": bool(await api.state_get("conversation", "runner.compaction.checkpoint"))})
        loop = AgentLoop(model_adapter=LangBotModelAdapter(api, remove_think=assembly.remove_think,
            reasoning_levels=model_reasoning_levels(config)), tool_executor=assembly.tool_executor,
            model_ids=assembly.model_ids, messages=assembly.messages, tools=assembly.tools, streaming=False,
            max_tool_iterations=assembly.max_tool_iterations, tool_execution_mode=assembly.tool_execution_mode,
            hooks=assembly.hooks)
        from langbot_plugin.api.entities.builtin.provider.message import Message
        from qunling.dialogue_review import RunReview
        from langbot_plugin.api.entities.builtin.runner import RunnerResult
        reviewer = RunReview(query)
        api.append(Message(role="user", content=target.text), target=target)
        response = None
        async with asyncio.timeout(min(int(config.get("timeout") or 600), 600)):
            async for event in loop.run():
                if str(event.type) == "run_failed":
                    raise RuntimeError("LocalAgent runtime replay failed")
                if str(event.type) == "message_end" and event.message:
                    if event.message.role == "assistant" and not event.message.tool_calls:
                        response = event.message
                    else:
                        api.append(event.message)
                if str(event.type) == "tool_execution_start":
                    await reviewer.observe(RunnerResult.tool_call_started("replay", tool_call_id=event.tool_call_id,
                        tool_name=event.tool_name, parameters=event.parameters).model_dump(mode="json"))
                if str(event.type) == "tool_execution_end":
                    self.tool_calls.append({"case": case["id"], "tool": event.tool_name,
                                            "parameters": event.parameters, "error": bool(event.error)})
                    payload = event.result if isinstance(event.result, dict) else {"value": event.result}
                    await reviewer.observe(RunnerResult.tool_call_completed("replay", tool_call_id=event.tool_call_id,
                        tool_name=event.tool_name, result=payload, error=event.error).model_dump(mode="json"))
        if response is None:
            raise ValueError("LocalAgent did not produce a final answer")
        result = await reviewer.observe(RunnerResult.message_completed("replay", response).model_dump(mode="json"))
        response = Message(**result["data"]["message"])
        api.append(response)
        return response

    def close(self):
        self.history.close()


class ReplayAPI:
    """Host API boundary with private persisted history and no platform dispatch."""
    def __init__(self, provider, conversation, query, target, case):
        self.provider, self.conversation, self.query, self.target, self.case = provider, conversation, query, target, case
        self.query_scope = NS(query_id=target.message_id, query_uuid=target.message_id,
                              variables={"user_message_text": target.text, "sender_id": target.sender_id,
                                         "sender_name": target.sender_name})
        self.items = [json.loads(row[0]) for row in provider.history.execute(
            "SELECT item FROM turns WHERE conversation=? ORDER BY seq", (conversation,))]
        if not self.items and case.get("history"):
            from langbot_plugin.api.entities.builtin.provider.message import Message
            for index, (actor, text) in enumerate(case["history"]):
                prior = NS(sender_id=provider.actor_id(actor), message_id="history-" + str(index),
                           timestamp=1700000000 + index)
                self.append(Message(role="assistant" if actor == "bot" else "user", content=text),
                            target=None if actor == "bot" else prior)
            self.items = [json.loads(row[0]) for row in provider.history.execute(
                "SELECT item FROM turns WHERE conversation=? ORDER BY seq", (conversation,))]
        import yaml
        self.tool_results = dict(case.get("tool_results", {}))
        if case.get("evidence"):
            self.tool_results.setdefault("search_web", {"source": "recorded-replay-observation", "text": case["evidence"]})
        allowed = set(case["tools"]) if "tools" in case else None
        if case.get("memories") or case.get("profiles"):
            if allowed is not None:
                allowed.add("recall_memory")
        self.tools = []
        root = Path(__file__).resolve().parents[1]
        for path in (root / "custom-plugins").glob("*/components/tools/*.yaml"):
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
            metadata, spec = value.get("metadata", {}), value.get("spec", {})
            name = metadata.get("name")
            if allowed is not None and name not in allowed:
                continue
            if path.parents[2].name == "LongTermMemory" and not (case.get("memories") or case.get("profiles")):
                continue
            manifest = yaml.safe_load((path.parents[2] / "manifest.yaml").read_text(encoding="utf-8"))
            identity = manifest["metadata"]["author"] + "/" + manifest["metadata"]["name"]
            description = metadata.get("description", {})
            if isinstance(description, dict):
                description = description.get("zh_Hans") or description.get("en_US") or ""
            self.tools.append(NS(tool_name=name, parameters=spec.get("parameters", {}),
                description=description + "\n" + spec.get("llm_prompt", ""), source="plugin",
                source_id=identity,
                operations=["call"]))

    def append(self, message, target=None):
        item = {"role": message.role, "content_json": message.model_dump(exclude_none=True),
                "event_id": target.message_id if target else "generated", "metadata": {
                    "actor_id": target.sender_id, "platform_message_id": target.message_id,
                    "event_time": target.timestamp} if target else {}}
        cursor = self.provider.history.execute("INSERT INTO turns(conversation,item) VALUES (?,?)",
                                               (self.conversation, json.dumps(item, ensure_ascii=False))).lastrowid
        item["cursor"] = str(cursor)
        self.provider.history.execute("UPDATE turns SET item=? WHERE seq=?", (json.dumps(item, ensure_ascii=False), cursor))
        self.provider.history.commit()

    def get_allowed_models(self):
        return [NS(model_id=key) for key in self.provider.model_rows]

    def get_allowed_tools(self):
        return self.tools

    def get_allowed_knowledge_bases(self):
        return [NS(kb_id="replay-memory")] if self.case.get("memories") or self.case.get("profiles") else []

    async def get_prompt(self):
        return [message.model_dump(exclude_none=True) for message in self.query.prompt.messages]

    async def history_page(self, **kwargs):
        items = self.items
        if kwargs.get("after_cursor"):
            items = [item for item in items if int(item["cursor"]) > int(kwargs["after_cursor"])]
        if kwargs.get("direction") == "backward":
            items = list(reversed(items))
        page = items[:kwargs.get("limit", 50)]
        return {"items": page, "has_more": len(items) > len(page), "next_cursor": page[-1]["cursor"] if page else None}

    async def state_get(self, scope, key):
        row = self.provider.history.execute("SELECT value FROM state WHERE conversation=? AND key=?",
                                            (self.conversation, key)).fetchone()
        return {"value": json.loads(row[0])} if row else {}

    async def state_set(self, scope, key, value):
        self.provider.history.execute("INSERT OR REPLACE INTO state VALUES (?,?,?)",
                                      (self.conversation, key, json.dumps(value, ensure_ascii=False)))
        self.provider.history.commit()

    async def count_tokens(self, llm_model_uuid, messages, funcs=None, reasoning_level=None):
        model = self.provider.model(llm_model_uuid, reasoning_level)
        return await model.provider.requester.count_tokens(model, messages, funcs or [])

    async def invoke_llm(self, llm_model_uuid, messages, funcs=None, reasoning_level=None, **kwargs):
        return await self.provider.invoke(messages=messages, model_id=llm_model_uuid,
            funcs=funcs, reasoning_level=reasoning_level, **kwargs)

    async def retrieve_knowledge(self, kb_id, query_text, top_k=3, **_):
        if kb_id != "replay-memory":
            raise ValueError("Unbound replay knowledge base")
        entries = await self.provider.memory.retrieve(query_text, self.target.sender_id, profiles=True)
        return [entry.model_dump(exclude_none=True) for entry in entries]

    async def call_tool(self, tool_name, parameters=None, **kwargs):
        import jsonschema
        name = tool_name
        parameters = parameters or {}
        tool = next((item for item in self.tools if item.tool_name == name), None)
        if tool is None:
            raise ValueError("Tool is not authorized in this isolated replay")
        jsonschema.validate(parameters, tool.parameters)
        if name == "recall_memory":
            return await self.provider.memory.engine.plugin.memory_store.search_episodes(
                self.provider.memory.collection, "local-bge", parameters["query"],
                user_key="synthetic-bot:group_900000001", top_k=min(parameters.get("top_k", 3), 10),
                sender_id=parameters.get("speaker_id", ""), sender_name=parameters.get("speaker_name", ""))
        # Recorded observations replay the external boundary; no QQ, search,
        # file delivery or other external action is dispatched from this API.
        return copy.deepcopy(self.tool_results.get(name, {"status": "unavailable",
            "reason": "This isolated replay has no recorded result for this tool; no external action was performed."}))


class ReplayMemory:
    """Real Chinese embedding + Chroma + LongTermMemory retrieval on fixture data."""
    def __init__(self, provider, case):
        import importlib.util
        from qunling.embeddings import MODEL, embed
        from langbot.pkg.vector.vdbs.chroma import ChromaVectorDatabase

        root = Path(__file__).resolve().parents[1] / "custom-plugins/LongTermMemory"
        sys.path.insert(0, str(root))
        from store.memory_store import MemoryStore
        spec = importlib.util.spec_from_file_location("replay_memory_engine", root / "components/knowledge_engine/memory_engine.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.provider, self.case = provider, case
        self.collection = "replay_" + hashlib.sha256(case["id"].encode()).hexdigest()[:32]
        self.embedding = lambda texts: embed(MODEL, texts)
        self.vector = ChromaVectorDatabase(NS(instance_config=NS(data={}), logger=provider.ap.logger),
                                          str(provider.output_dir / "chroma"))
        self.storage = {}
        self.memory_store = MemoryStore(self)
        self.engine = module.LongTermMemoryEngine()
        self.engine.plugin = self
        self.plugin_runtime_handler = None

    async def get_plugin_storage(self, key):
        return self.storage[key]

    async def get_plugin_storage_keys(self):
        return list(self.storage)

    async def set_plugin_storage(self, key, value):
        self.storage[key] = value

    async def inject(self, api):
        """Run the actual event listener; substitute only its RPC transport."""
        import importlib.util
        from unittest.mock import patch
        root = Path(__file__).resolve().parents[1] / "custom-plugins/LongTermMemory"
        spec = importlib.util.spec_from_file_location("replay_memory_injector", root / "components/event_listener/memory_injector.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        memory = self
        class QueryAPI:
            async def list_pipeline_knowledge_bases(self):
                return [{"uuid": "replay-memory"}]
            async def get_query_vars(self):
                return api.query_scope.variables
            async def set_query_var(self, key, value):
                api.query_scope.variables[key] = value
            async def get_bot_uuid(self):
                return "synthetic-bot"
            async def retrieve_knowledge(self, **kwargs):
                # The Host's automatic L2 recall does not duplicate L1 profiles.
                entries = await memory.retrieve(kwargs["query_text"], api.target.sender_id, profiles=False)
                return [entry.model_dump(exclude_none=True) for entry in entries]
        event = NS(default_prompt=api.query.prompt.messages, prompt=[], session_name="group_900000001")
        injector = module.MemoryInjector()
        injector.plugin = self
        with patch.object(module, "QueryBasedAPIProxy", return_value=QueryAPI()):
            await injector._inject_profile(NS(event=event, query_id=api.target.message_id, query_uuid=api.target.message_id))
        api.query.prompt.messages.extend(event.prompt)

    async def invoke_embedding(self, model, texts):
        return await asyncio.to_thread(self.embedding, texts)

    async def vector_search(self, collection_id, query_vector, top_k=3, filters=None, **kwargs):
        if collection_id != self.collection:
            raise ValueError("Foreign replay collection")
        result = await self.vector.search(collection_id, query_vector, k=top_k, filter=filters, **kwargs)
        return [{"id": item, "distance": result["distances"][0][index], "metadata": result["metadatas"][0][index]}
                for index, item in enumerate(result["ids"][0])]

    async def vector_list(self, collection_id, filters=None, limit=20, offset=0):
        if collection_id != self.collection:
            raise ValueError("Foreign replay collection")
        return await self.vector.list_by_filter(collection_id, filter=filters, limit=limit, offset=offset)

    async def seed(self):
        self.storage["kb_configs"] = json.dumps({"replay-memory": {
            "embedding_model_uuid": "local-bge", "isolation": "session"}}).encode()
        for actor, profile in self.case.get("profiles", {}).items():
            key = ("ps:synthetic-bot:group_900000001" if actor == "group" else
                   "pp:synthetic-bot:group_900000001:" + self.provider.actor_id(actor))
            self.storage[key] = json.dumps(profile, ensure_ascii=False).encode()
        entries = self.case.get("memories", [])
        if entries:
            texts = [entry["content"] for entry in entries]
            vectors = await self.invoke_embedding("local-bge", texts)
            metadata = [{"content": text, "user_key": "synthetic-bot:group_" + entry.get("group", "900000001"),
                "sender_id": self.provider.actor_id(entry.get("actor", "a")), "status": entry.get("status", "active"),
                "importance": "2", "timestamp": "2026-10-25T12:00:00Z"} for text, entry in zip(texts, entries)]
            await self.vector.add_embeddings(self.collection, ["episode-" + str(i) for i in range(len(entries))],
                                             vectors, metadata, texts)

    async def retrieve(self, text, sender, *, profiles=True, episodes=True):
        from langbot_plugin.api.entities.builtin.rag.context import RetrievalContext
        episodes = episodes and bool(self.case.get("memories"))
        if not profiles and not episodes:
            return []
        response = await self.engine.retrieve(RetrievalContext(query=text, knowledge_base_id=self.collection,
            creation_settings={"embedding_model_uuid": "local-bge", "isolation": "session"},
            retrieval_settings={"bot_uuid": "synthetic-bot", "session_name": "group_900000001", "sender_id": sender,
                "include_profiles": profiles, "include_episodes": episodes, "top_k": 3}))
        return response.results
