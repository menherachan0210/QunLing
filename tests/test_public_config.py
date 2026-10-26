"""Offline setup contract tests; no running service, network, or real secrets."""

from copy import deepcopy
import hashlib
import json

import pytest

from bootstrap import api as api_module
from bootstrap import configure as setup
from bootstrap.api import LangBotAPI, NapCatAPI, SetupError
from bootstrap.state import State


def settings(**changes):
    values = {
        "base_url": "https://api.example.com/v1/",
        "api_key": "synthetic-key",
        "chat_model": "chat-example",
        "vision_model": "vision-example",
        "image_model": "image-example",
        "search_model": "search-example",
    }
    return setup.settings_from_input({**values, **changes})


class FakeLangBot:
    """In-memory shapes from LangBot 4.11 controller/service contracts."""

    collections = {
        "/api/v1/provider/providers": "providers",
        "/api/v1/provider/models/llm": "models",
        "/api/v1/provider/models/embedding": "models",
        "/api/v1/knowledge/bases": "bases",
        "/api/v1/pipelines": "pipelines",
        "/api/v1/agents": "agents",
        "/api/v1/platform/bots": "bots",
    }

    def __init__(self):
        self.records = {endpoint: [] for endpoint in self.collections}
        self.configs = {
            identity: {"unrelated_option": "preserve-me"}
            for identity in setup.PLUGINS
        }
        self.calls = []
        self.counter = 0
        self.memory_records = ["synthetic-memory-that-must-survive"]
        self.mask_secrets = False
        self.installations = []
        self.plugins = [
            {
                "manifest": {"manifest": {"metadata": {
                    "author": identity.split("/")[0], "name": identity.split("/")[1],
                }}},
                "status": "initialized",
                "enabled": True,
            }
            for identity in sorted(setup.PLUGINS)
        ]

    def call(self, method, path, data=None):
        self.calls.append((method, path, deepcopy(data)))
        if path == "/api/v1/plugins/_/installations":
            assert method == "GET"
            return {"installations": deepcopy(self.installations)}
        if path == "/api/v1/plugins":
            assert method == "GET"
            return {"plugins": deepcopy(self.plugins)}
        if path.startswith("/api/v1/plugins/") and path.endswith("/config"):
            identity = path[len("/api/v1/plugins/") : -len("/config")]
            if method == "GET":
                config = deepcopy(self.configs[identity])
                if self.mask_secrets:
                    for key in ("api_key", "openai_api_key"):
                        if config.get(key):
                            config[key] = "***"
                return {"config": config}
            assert method == "PUT"
            self.configs[identity] = deepcopy(data)
            return {}
        if path in self.collections:
            if method == "GET":
                return {self.collections[path]: deepcopy(self.records[path])}
            assert method == "POST"
            self.counter += 1
            row = {"uuid": f"synthetic-resource-{self.counter}", **deepcopy(data)}
            if path == "/api/v1/pipelines":
                # LangBot create_pipeline fills its template and defaults.
                row.update({
                    "config": {
                        "trigger": {"ignore-rules": {"prefix": [], "regexp": []}},
                        "ai": {"runner": {"id": ""}, "runner_config": {}},
                        "output": {"misc": {"quote-origin": True}},
                    },
                    "extensions_preferences": {"enable_all_plugins": True},
                })
            self.records[path].append(row)
            return {"uuid": row["uuid"]}
        endpoint, uuid = path.rsplit("/", 1)
        assert endpoint in self.records, (method, path)
        row = next(r for r in self.records[endpoint] if r["uuid"] == uuid)
        if method == "GET":
            assert endpoint in {"/api/v1/pipelines", "/api/v1/platform/bots"}
            return {"pipeline" if endpoint.endswith("pipelines") else "bot": deepcopy(row)}
        assert method == "PUT"
        row.update(deepcopy(data))
        return {}


def run_configure(api, config=None, record=None):
    saved = []
    result = setup.configure(
        api, config or settings(), record or {}, saved.append,
        "synthetic-onebot-token", "http://langbot:5300", "/opt/tools/7zz",
    )
    assert saved[-1] == result
    return result


def row(api, endpoint, uuid):
    return next(v for v in api.records[endpoint] if v["uuid"] == uuid)


class ReadinessClock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


def test_plugin_readiness_requires_all_seven_in_the_same_snapshot():
    api = FakeLangBot()
    ready = deepcopy(api.plugins)
    missing = ready.pop()
    api.plugins = ready
    api.plugins[0]["status"] = "starting"
    clock = ReadinessClock()

    def advance(delay):
        clock.sleep(delay)
        if len(clock.sleeps) == 1:
            # One recovers while another temporarily disappears during restart.
            api.plugins[0]["status"] = "initialized"
            api.plugins.append(missing)
            api.plugins[1]["status"] = "mounted"
        else:
            api.plugins[1]["status"] = "initialized"

    setup.wait_for_plugins_initialized(api, sleep=advance, clock=clock.monotonic)
    assert clock.sleeps == [2, 2]
    assert api.calls == [("GET", "/api/v1/plugins", None)] * 3


@pytest.mark.parametrize("change", [{"status": "failed"}, {"enabled": False}])
def test_plugin_readiness_fails_early_without_exposing_runtime_details(change):
    api = FakeLangBot()
    api.plugins[0].update(change, error_message="synthetic-private-runtime-details")
    clock = ReadinessClock()
    with pytest.raises(SetupError, match="插件启动失败") as error:
        setup.wait_for_plugins_initialized(api, sleep=clock.sleep, clock=clock.monotonic)
    assert "synthetic-private-runtime-details" not in str(error.value)
    assert clock.sleeps == []
    assert api.calls == [("GET", "/api/v1/plugins", None)]


def test_configure_waits_after_put_before_creating_processors(monkeypatch):
    api = FakeLangBot()
    clock = ReadinessClock()
    waiter = setup.wait_for_plugins_initialized
    api.plugins[0]["status"] = "starting"

    def advance(delay):
        assert not api.records["/api/v1/agents"]
        assert not api.records["/api/v1/platform/bots"]
        puts = [call for call in api.calls if call[0] == "PUT" and "/plugins/" in call[1]]
        assert len(puts) == 4
        clock.sleep(delay)
        api.plugins[0]["status"] = "initialized"

    monkeypatch.setattr(
        setup, "wait_for_plugins_initialized",
        lambda api: waiter(api, sleep=advance, clock=clock.monotonic),
    )
    result = run_configure(api)
    assert result["configured"]
    assert clock.sleeps == [2]
    polls = [i for i, call in enumerate(api.calls) if call[1] == "/api/v1/plugins"]
    assert all(call[0] == "GET" for call in api.calls[polls[0]:polls[-1] + 1])
    assert len(api.records["/api/v1/agents"]) == 2


def test_readiness_timeout_preserves_progress_and_retry_does_not_rewrite_plugins(monkeypatch):
    api = FakeLangBot()
    api.plugins = []
    clock = ReadinessClock()
    waiter = setup.wait_for_plugins_initialized
    monkeypatch.setattr(
        setup, "wait_for_plugins_initialized",
        lambda api: waiter(api, timeout=3, sleep=clock.sleep, clock=clock.monotonic),
    )
    saved = []
    with pytest.raises(SetupError, match="等待插件启动超时"):
        setup.configure(
            api, settings(), {}, saved.append,
            "synthetic-token", "http://langbot:5300", "/opt/tools/7zz",
        )
    assert clock.sleeps == [2, 1]
    assert saved[-1]["memory_uuid"] and saved[-1]["pipeline_uuid"]
    assert not saved[-1].get("configured")
    assert not api.records["/api/v1/agents"]
    assert not api.records["/api/v1/platform/bots"]
    polls = [i for i, call in enumerate(api.calls) if call[1] == "/api/v1/plugins"]
    assert len(polls) == 3
    assert all(call[0] == "GET" for call in api.calls[polls[0]:])
    memory_id = saved[-1]["memory_uuid"]
    api.plugins = FakeLangBot().plugins
    api.calls.clear()
    result = run_configure(api, record=saved[-1])
    assert result["configured"] and result["memory_uuid"] == memory_id
    assert len(api.records["/api/v1/knowledge/bases"]) == 1
    assert not any(call[0] == "PUT" and "/plugins/" in call[1] for call in api.calls)


def test_empty_install_configures_distinct_model_roles_memory_processors_and_bot():
    api = FakeLangBot()
    result = run_configure(api)
    assert result["chat_model_uuid"] != result["vision_model_uuid"]
    pipeline = row(api, "/api/v1/pipelines", result["pipeline_uuid"])
    runner = pipeline["config"]["ai"]["runner_config"][setup.RUNNER]
    assert runner["model"]["primary"] == result["chat_model_uuid"]
    assert runner["knowledge-bases"] == [result["memory_uuid"]]
    assert runner["box-enabled"] is False and runner["timeout"] >= 600
    assert pipeline["config"]["trigger"]["group-respond-rules"] == {
        "at": True, "prefix": ["/ai"], "regexp": [], "random": 0.0,
    }
    assert pipeline["config"]["output"]["misc"]["quote-origin"] is True
    assert pipeline["extensions_preferences"]["enable_all_plugins"] is True
    memory = row(api, "/api/v1/knowledge/bases", result["memory_uuid"])
    assert memory["knowledge_engine_plugin_id"] == "langbot-team/LongTermMemory"
    assert memory["creation_settings"]["embedding_model_uuid"] == result["embedding_uuid"]
    assert memory["creation_settings"]["isolation"] == "session"
    assert not memory["creation_settings"]["candidate_auto_apply"]
    embedding_provider = row(api, "/api/v1/provider/providers", result["embedding_provider_uuid"])
    assert embedding_provider["requester"] == "chroma-embedding"
    assert embedding_provider["api_keys"] == []
    assert {a["component_ref"] for a in api.records["/api/v1/agents"]} == {
        "plugin:local/QQTextReader/group_upload",
        "plugin:local/QQComicTranslator/group_upload",
    }
    assert all(a["kind"] == "event_processor" for a in api.records["/api/v1/agents"])
    bot = row(api, "/api/v1/platform/bots", result["bot_uuid"])
    assert bot["adapter"] == "onebot-v11"
    assert bot["adapter_config"]["connection_mode"] == "reverse_ws"
    assert bot["event_bindings"][0]["target_uuid"] == result["pipeline_uuid"]
    assert bot["event_bindings"][0]["filters"] == []
    assert len(bot["plugin_processors"]) == 2
    assert all(p["enabled"] for p in bot["plugin_processors"])
    assert result["onebot_url"] == f"ws://langbot:5300/bots/{result['bot_uuid']}/onebot/v11/ws"
    for identity in ("local/QQGroupContext", "local/QQComicTranslator"):
        assert api.configs[identity]["model_uuid"] == result["vision_model_uuid"]
    assert api.configs["local/QQWebSearch"]["vision_model_uuid"] == result["vision_model_uuid"]
    for identity in ("local/QQGroupContext", "langbot-team/AIImagePlugin"):
        assert api.configs[identity]["history_hours"] == 24
        assert api.configs[identity]["history_max_messages"] == 1000
    assert all(c["unrelated_option"] == "preserve-me" for c in api.configs.values())


def test_rerun_reuses_resources_keeps_memories_and_preserves_custom_settings():
    api = FakeLangBot()
    first = run_configure(api)
    count = api.counter
    pipeline = row(api, "/api/v1/pipelines", first["pipeline_uuid"])
    config = pipeline["config"]
    config["trigger"]["group-respond-rules"]["prefix"] = ["/custom"]
    runner = config["ai"]["runner_config"][setup.RUNNER]
    runner["knowledge-bases"].append("unrelated-memory-uuid")
    runner["prompt"].append({"role": "system", "content": "Synthetic custom instruction."})
    bot = row(api, "/api/v1/platform/bots", first["bot_uuid"])
    bot["plugin_processors"].append({"processor_uuid": "unrelated-processor", "enabled": False})
    original_record = deepcopy(first)
    result = run_configure(api, record=first)
    assert first == original_record and result == first
    assert api.counter == count
    assert api.memory_records == ["synthetic-memory-that-must-survive"]
    assert not any(method == "DELETE" for method, _, _ in api.calls)
    config = row(api, "/api/v1/pipelines", result["pipeline_uuid"])["config"]
    assert config["trigger"]["group-respond-rules"]["prefix"] == ["/custom"]
    runner = config["ai"]["runner_config"][setup.RUNNER]
    assert runner["knowledge-bases"].count(first["memory_uuid"]) == 1
    assert "unrelated-memory-uuid" in runner["knowledge-bases"]
    assert {"role": "system", "content": "Synthetic custom instruction."} in runner["prompt"]
    assert sum(p["content"].startswith("[QQBOT SETUP") for p in runner["prompt"]) == 1
    processors = row(api, "/api/v1/platform/bots", first["bot_uuid"])["plugin_processors"]
    assert len(processors) == 3
    assert {"processor_uuid": "unrelated-processor", "enabled": False} in processors


def test_changed_key_and_url_propagate_to_chat_image_and_search_without_new_memory():
    api = FakeLangBot()
    first = run_configure(api)
    revised = settings(api_key="synthetic-new-key", base_url="https://new.example.com/v1")
    second = run_configure(api, revised, first)
    provider = row(api, "/api/v1/provider/providers", second["provider_uuid"])
    assert provider["api_keys"] == [revised["api_key"]]
    assert provider["base_url"] == revised["base_url"]
    assert api.configs["langbot-team/AIImagePlugin"]["openai_api_key"] == revised["api_key"]
    assert api.configs["local/QQWebSearch"]["api_key"] == revised["api_key"]
    assert api.configs["langbot-team/AIImagePlugin"]["api_base_url"] == revised["base_url"]
    assert api.configs["local/QQWebSearch"]["api_base_url"] == revised["base_url"]
    assert second["memory_uuid"] == first["memory_uuid"]
    assert len(api.records["/api/v1/knowledge/bases"]) == 1


def test_unchanged_rerun_with_redacted_keys_does_not_reload_plugins_or_bot():
    api = FakeLangBot()
    first = run_configure(api)
    api.mask_secrets = True
    api.calls.clear()
    second = run_configure(api, record=first)
    assert second == first
    assert all(method == "GET" for method, _, _ in api.calls)


def test_key_rotation_with_redacted_keys_updates_only_affected_plugins():
    api = FakeLangBot()
    first = run_configure(api)
    api.mask_secrets = True
    api.calls.clear()
    changed = settings(api_key="synthetic-rotated-key")
    run_configure(api, changed, first)
    updated_plugins = {
        path for method, path, _ in api.calls
        if method == "PUT" and path.startswith("/api/v1/plugins/")
    }
    assert updated_plugins == {
        "/api/v1/plugins/langbot-team/AIImagePlugin/config",
        "/api/v1/plugins/local/QQWebSearch/config",
    }
    assert api.configs["langbot-team/AIImagePlugin"]["openai_api_key"] == changed["api_key"]
    assert api.configs["local/QQWebSearch"]["api_key"] == changed["api_key"]


def test_changed_visible_plugin_setting_is_repaired_without_duplicate_resources():
    api = FakeLangBot()
    first = run_configure(api)
    api.mask_secrets = True
    api.configs["local/QQGroupContext"]["model_uuid"] = "synthetic-wrong-model"
    count = api.counter
    api.calls.clear()
    run_configure(api, record=first)
    assert api.counter == count
    assert api.configs["local/QQGroupContext"]["model_uuid"] == first["vision_model_uuid"]
    assert [path for method, path, _ in api.calls if method == "PUT"] == [
        "/api/v1/plugins/local/QQGroupContext/config",
    ]


def test_same_chat_and_vision_model_can_reuse_one_resource():
    api = FakeLangBot()
    result = run_configure(api, settings(vision_model="chat-example"))
    assert result["chat_model_uuid"] == result["vision_model_uuid"]
    assert len(api.records["/api/v1/provider/models/llm"]) == 1


def test_input_retains_saved_key_but_requires_complete_image_configuration():
    previous = settings()
    result = setup.settings_from_input({**previous, "api_key": "", "vision_model": "", "search_model": ""}, previous)
    assert result["api_key"] == previous["api_key"]
    assert result["vision_model"] == result["search_model"] == result["chat_model"]
    with pytest.raises(SetupError):
        settings(image_model="")


@pytest.mark.parametrize("hours,messages", [(1, 100), (168, 1000), ("48", "750")])
def test_group_context_limits_accept_bounded_integers_and_browser_strings(hours, messages):
    config = settings(group_context_hours=hours, group_context_max_messages=messages)
    assert type(config["group_context_hours"]) is int
    assert type(config["group_context_max_messages"]) is int
    assert config["group_context_hours"] == int(hours)
    assert config["group_context_max_messages"] == int(messages)


@pytest.mark.parametrize("field,value", [
    ("group_context_hours", 0), ("group_context_hours", 169),
    ("group_context_hours", True), ("group_context_hours", 24.0),
    ("group_context_hours", "1.5"), ("group_context_hours", ""),
    ("group_context_hours", "24\ninvalid"), ("group_context_hours", None),
    ("group_context_max_messages", 99), ("group_context_max_messages", 1001),
    ("group_context_max_messages", False), ("group_context_max_messages", 1000.0),
    ("group_context_max_messages", "1e3"), ("group_context_max_messages", []),
])
def test_group_context_invalid_limits_are_rejected(field, value):
    with pytest.raises(SetupError):
        settings(**{field: value})


def test_context_only_change_keeps_saved_ai_settings_and_updates_both_plugins():
    api = FakeLangBot()
    previous = settings()
    first = run_configure(api, previous)
    count = api.counter
    api.calls.clear()
    config = setup.settings_from_input(
        {"group_context_hours": "72", "group_context_max_messages": "600"}, previous
    )
    for key in ("base_url", "api_key", "chat_model", "vision_model", "image_model", "search_model"):
        assert config[key] == previous[key]
    second = run_configure(api, config, first)
    assert api.counter == count and second["memory_uuid"] == first["memory_uuid"]
    assert {
        path for method, path, _ in api.calls if method == "PUT"
    } == {
        "/api/v1/plugins/local/QQGroupContext/config",
        "/api/v1/plugins/langbot-team/AIImagePlugin/config",
    }
    for identity in ("local/QQGroupContext", "langbot-team/AIImagePlugin"):
        assert api.configs[identity]["history_hours"] == 72
        assert api.configs[identity]["history_max_messages"] == 600
    assert api.configs["langbot-team/AIImagePlugin"]["openai_api_key"] == previous["api_key"]


def test_old_saved_settings_get_defaults_on_restore_and_public_status():
    old = {key: value for key, value in settings().items() if key not in setup.GROUP_CONTEXT_SETTINGS}
    assert setup.public_settings(old)["group_context_hours"] == 24
    assert setup.public_settings(old)["group_context_max_messages"] == 1000
    api = FakeLangBot()
    run_configure(api, old)
    for identity in ("local/QQGroupContext", "langbot-team/AIImagePlugin"):
        assert api.configs[identity]["history_hours"] == 24
        assert api.configs[identity]["history_max_messages"] == 1000


def test_older_client_omitting_context_fields_keeps_existing_custom_limits():
    previous = settings(group_context_hours=96, group_context_max_messages=900)
    updated = setup.settings_from_input({"chat_model": "replacement-chat"}, previous)
    assert updated["group_context_hours"] == 96
    assert updated["group_context_max_messages"] == 900
    assert updated["api_key"] == previous["api_key"]
    assert updated["image_model"] == previous["image_model"]


def test_context_setting_persistence_and_redacted_status_roundtrip(tmp_path, monkeypatch):
    from bootstrap.server import SetupManager
    from bootstrap.state import prepare

    monkeypatch.delenv("QQBOT_UPDATER_URL", raising=False)
    prepare(tmp_path / "private", tmp_path / "napcat")
    manager = SetupManager(
        State(tmp_path / "private"), "http://app:5300", "http://napcat:6099",
        tmp_path, "7zz",
    )
    manager.api = FakeLangBot()
    original = settings()
    manager._apply(original)
    manager.update(stage="ready", plugins_ready=True)
    manager.submit({"group_context_hours": "48", "group_context_max_messages": "800"})
    pending, manager.pending = manager.pending, None
    manager._apply(pending)
    saved = manager.state.read("ai.json")
    assert saved["group_context_hours"] == 48
    assert saved["group_context_max_messages"] == 800
    assert saved["api_key"] == original["api_key"]
    for key in ("chat_model", "image_model", "vision_model", "search_model"):
        assert saved[key] == original[key]
    visible = manager.snapshot()["settings"]
    assert visible["group_context_hours"] == 48
    assert visible["group_context_max_messages"] == 800
    assert visible["api_key_saved"] is True
    assert "api_key" not in visible and original["api_key"] not in json.dumps(visible)
    assert setup.settings_from_input(saved) == saved


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "https://user:password@example.com/v1",
    "https://example.com:99999/v1", "https://example.com/v1?secret=value",
    "https://example.com/v1#fragment", "https://example.com/\nheader",
])
def test_invalid_provider_urls_are_rejected(url):
    with pytest.raises(SetupError):
        settings(base_url=url)


def test_public_settings_uses_allowlist_for_future_private_fields():
    config = settings()
    config.update({"onebot_token": "synthetic-private", "credentials": {"password": "synthetic-private"}})
    public = setup.public_settings(config)
    assert set(public) == {
        "base_url", "chat_model", "vision_model", "image_model", "search_model", "api_key_saved",
        "group_context_hours", "group_context_max_messages",
        "image_size", "image_quality", "image_timeout_seconds", "search_provider",
        "search_base_url", "search_api_key_saved", "search_timeout_seconds",
    }
    assert public["api_key_saved"] is True
    assert "synthetic-private" not in json.dumps(public)
    assert config["api_key"] not in json.dumps(public)


@pytest.mark.parametrize("field,value", [
    ("image_size", "unbounded"), ("image_quality", "ultra"), ("image_quality", []),
    ("image_timeout_seconds", 29), ("image_timeout_seconds", 301),
    ("image_timeout_seconds", True), ("search_timeout_seconds", "1e3"),
    ("search_timeout_seconds", 91), ("search_timeout_seconds", 29),
    ("search_provider", "other"), ("search_base_url", "https://user:secret@example.com"),
])
def test_tool_settings_reject_invalid_values(field, value):
    with pytest.raises(SetupError):
        settings(**{field: value})


def test_independent_search_roundtrip_and_switching_back_to_shared_preserves_credentials():
    api = FakeLangBot()
    original = settings(search_provider="independent", search_base_url="https://search.example.com/v1/",
                        search_api_key="synthetic-search-key", search_timeout_seconds="80",
                        image_size="1280x720", image_quality="high", image_timeout_seconds="180")
    first = run_configure(api, original)
    revised = setup.settings_from_input({"api_key": "new-common-key", "search_api_key": ""}, original)
    run_configure(api, revised, first)
    search = api.configs["local/QQWebSearch"]
    assert search["api_base_url"] == "https://search.example.com/v1"
    assert search["api_key"] == original["search_api_key"]
    assert search["request_timeout_seconds"] == 80
    image = api.configs["langbot-team/AIImagePlugin"]
    assert (image["image_size"], image["quality"], image["request_timeout_seconds"]) == ("1280x720", "high", 180)
    public = setup.public_settings(revised)
    assert public["search_api_key_saved"] is True
    assert original["search_api_key"] not in json.dumps(public)
    shared = setup.settings_from_input({"search_provider": "shared"}, revised)
    run_configure(api, shared, first)
    assert api.configs["local/QQWebSearch"]["api_key"] == "new-common-key"
    assert shared["search_api_key"] == original["search_api_key"]
    restored = setup.settings_from_input({"search_provider": "independent"}, shared)
    run_configure(api, restored, first)
    assert api.configs["local/QQWebSearch"]["api_key"] == original["search_api_key"]


def test_legacy_plugin_settings_are_adopted_before_console_defaults():
    api = FakeLangBot()
    legacy = {key: value for key, value in settings().items() if key not in setup.TOOL_FIELDS}
    api.configs["langbot-team/AIImagePlugin"].update(image_size="768x1280", quality="medium", request_timeout_seconds=120)
    api.configs["local/QQWebSearch"].update(api_base_url="https://search.example.com/v1", api_key="legacy-search-key", model_name="legacy-search-model", request_timeout_seconds=88)
    adopted = setup.settings_from_input(setup.adopt_tool_settings(api, legacy))
    assert adopted["image_size"] == "768x1280"
    assert adopted["image_quality"] == "medium"
    assert adopted["image_timeout_seconds"] == 120
    assert adopted["search_provider"] == "independent"
    assert adopted["search_api_key"] == "legacy-search-key"
    assert adopted["search_model"] == "legacy-search-model"
    assert adopted["search_timeout_seconds"] == 88
    run_configure(api, legacy)
    assert api.configs["langbot-team/AIImagePlugin"]["quality"] == "medium"
    api.mask_secrets = True
    with pytest.raises(SetupError, match="密钥无法读取"):
        setup.adopt_tool_settings(api, legacy)


def test_ordinary_configure_preserves_advanced_runner_and_unmanaged_plugin_options():
    api = FakeLangBot()
    first = run_configure(api)
    runner = row(api, "/api/v1/pipelines", first["pipeline_uuid"])["config"]["ai"]["runner_config"][setup.RUNNER]
    runner["model"].update(fallbacks=["other-model"], reasoning={"effort": "high"})
    runner.update({"max-tool-iterations": 7, "timeout": 420, "box-enabled": True})
    run_configure(api, settings(chat_model="new-chat", image_quality="high"), first)
    runner = row(api, "/api/v1/pipelines", first["pipeline_uuid"])["config"]["ai"]["runner_config"][setup.RUNNER]
    assert runner["model"]["fallbacks"] == ["other-model"]
    assert runner["model"]["reasoning"] == {"effort": "high"}
    assert (runner["max-tool-iterations"], runner["timeout"], runner["box-enabled"]) == (7, 420, True)
    assert api.configs["langbot-team/AIImagePlugin"]["unrelated_option"] == "preserve-me"


def test_manager_persists_adopted_values_and_rejects_stale_revision(tmp_path, monkeypatch):
    from bootstrap.server import SetupManager
    from bootstrap.state import prepare
    monkeypatch.delenv("QQBOT_UPDATER_URL", raising=False)
    prepare(tmp_path / "private", tmp_path / "napcat")
    manager = SetupManager(State(tmp_path / "private"), "http://app:5300", "http://napcat:6099", tmp_path, "7zz")
    manager.api = FakeLangBot()
    manager.api.configs["langbot-team/AIImagePlugin"].update(quality="high", request_timeout_seconds=180)
    legacy = {key: value for key, value in settings().items() if key not in setup.TOOL_FIELDS}
    manager._apply(legacy)
    saved = manager.state.read("ai.json")
    assert saved["image_quality"] == "high" and saved["image_timeout_seconds"] == 180
    revision = manager.snapshot()["settings_revision"]
    manager.update(stage="ready", plugins_ready=True)
    manager.submit({"settings_revision": revision, "image_quality": "low"})
    manager._apply(manager.pending)
    manager.update(stage="ready")
    with pytest.raises(SetupError) as error:
        manager.submit({"settings_revision": revision, "image_quality": "medium"})
    assert error.value.status == 409
    assert manager.state.read("ai.json")["image_quality"] == "low"


def test_napcat_merge_preserves_unmanaged_connections_and_is_idempotent():
    class NapCat:
        def __init__(self):
            self.config = {"other": True, "network": {"httpServers": [{"name": "existing"}],
                "websocketClients": [{"name": "other-app", "url": "ws://other:8000"}]}}
            self.calls = []

        def call(self, path, data=None):
            self.calls.append((path, deepcopy(data)))
            if path == "/OB11Config/GetConfig":
                return deepcopy(self.config)
            assert path == "/OB11Config/SetConfig"
            assert set(data) == {"config"} and isinstance(data["config"], str)
            self.config = json.loads(data["config"])
            return {}

    napcat = NapCat()
    assert setup.link_napcat(napcat, "ws://langbot:5300/bots/test/onebot/v11/ws", "synthetic-token")
    assert napcat.config["other"] is True
    assert napcat.config["network"]["httpServers"] == [{"name": "existing"}]
    assert len(napcat.config["network"]["websocketClients"]) == 2
    assert not setup.link_napcat(napcat, "ws://langbot:5300/bots/test/onebot/v11/ws", "synthetic-token")
    assert sum(p == "/OB11Config/SetConfig" for p, _ in napcat.calls) == 1


def test_napcat_first_login_authenticates_even_with_short_system_uptime(monkeypatch):
    client = NapCatAPI("http://napcat:6099", "synthetic-token")
    monkeypatch.setattr(api_module.time, "monotonic", lambda: 12.0)
    calls = []

    def post(path, data=None):
        calls.append((path, data))
        return {"Credential": "synthetic-credential"}

    monkeypatch.setattr(client, "_post", post)
    client.login()
    assert calls == [("/auth/login", {"hash": hashlib.sha256(b"synthetic-token.napcat").hexdigest()})]
    assert client.http.headers["Authorization"] == "Bearer synthetic-credential"
    client.login()
    assert len(calls) == 1


def test_langbot_admin_creation_is_saved_before_api_and_rerun_reuses_it(tmp_path, monkeypatch):
    private = State(tmp_path)
    client = LangBotAPI("http://langbot:5300", private)
    calls = []
    initialized = False

    def call(method, path, data=None):
        nonlocal initialized
        calls.append((method, path, deepcopy(data)))
        if path.endswith("/init") and method == "GET":
            return {"initialized": initialized}
        if path.endswith("/init"):
            assert private.read("langbot-admin.json") == data
            initialized = True
            return {}
        assert path == "/api/v1/user/auth"
        return {"token": "synthetic-admin-token"}

    monkeypatch.setattr(client, "call", call)
    client.login()
    first = private.read("langbot-admin.json")
    client.login()
    assert private.read("langbot-admin.json") == first
    assert sum(method == "POST" and path.endswith("/init") for method, path, _ in calls) == 1
    assert client.http.headers["Authorization"] == "Bearer synthetic-admin-token"


def package_release(tmp_path):
    packages = []
    for index, identity in enumerate(sorted(setup.PLUGINS)):
        content = f"synthetic-package-{identity}".encode()
        name = f"plugin-{index}.lbpkg"
        (tmp_path / name).write_bytes(content)
        packages.append({"identity": identity, "filename": name, "sha256": hashlib.sha256(content).hexdigest()})
    (tmp_path / "packages.json").write_text(json.dumps({"packages": packages}), encoding="utf-8")
    return packages


def persistent_installation(item, *, enabled=True):
    author, name = item["identity"].split("/")
    return {
        "plugin_author": author,
        "plugin_name": name,
        "artifact_digest": item["sha256"],
        "enabled": enabled,
        "installation_uuid": "synthetic-installation-" + name,
        "runtime_revision": 1,
    }


class PackageInstaller(FakeLangBot):
    def __init__(self, packages):
        super().__init__()
        self.packages = packages
        self.plugins = []
        self.installed = []

    def call(self, method, path, data=None):
        if path.startswith("/api/v1/system/tasks/"):
            self.calls.append((method, path, deepcopy(data)))
            assert method == "GET"
            return {"runtime": {"state": "FINISHED", "exception": None}}
        return super().call(method, path, data)

    def install(self, path):
        item = next(p for p in self.packages if p["filename"] == path.name)
        author, name = item["identity"].split("/")
        self.installed.append(item["identity"])
        self.plugins = [
            p for p in self.plugins if setup.plugin_identity(p) != item["identity"]
        ]
        self.plugins.append({
            "manifest": {"manifest": {"metadata": {"author": author, "name": name}}},
            "status": "initialized",
            "install_info": {"_certification": {"artifact_digest": item["sha256"]}},
        })
        self.installations = [
            record for record in self.installations
            if (record["plugin_author"], record["plugin_name"]) != (author, name)
        ]
        self.installations.append(persistent_installation(item))
        return len(self.installed)


def test_install_seven_packages_localagent_first_and_skip_matching_initialized(tmp_path):
    packages = package_release(tmp_path)
    api = PackageInstaller(packages)
    setup.ensure_plugins(api, tmp_path, sleep=lambda _: None)
    assert api.installed[0] == "langbot-team/LocalAgent"
    assert set(api.installed) == setup.PLUGINS and len(api.installed) == 7
    setup.ensure_plugins(api, tmp_path, sleep=lambda _: None)
    assert len(api.installed) == 7


def restarting_installer(packages):
    api = FakeLangBot()
    api.installations = [persistent_installation(item) for item in packages]
    by_identity = {item["identity"]: item for item in packages}
    for plugin in api.plugins:
        item = by_identity[setup.plugin_identity(plugin)]
        plugin.update({
            "status": "starting",
            "install_info": {"_certification": {"artifact_digest": item["sha256"]}},
        })
    api.install = lambda path: pytest.fail("Matching packages must never be reinstalled")
    return api


def test_restart_waits_for_matching_package_instead_of_reinstalling(tmp_path):
    api = restarting_installer(package_release(tmp_path))
    clock = ReadinessClock()

    def recover(delay):
        clock.sleep(delay)
        for plugin in api.plugins:
            plugin["status"] = "initialized"

    setup.ensure_plugins(api, tmp_path, sleep=recover)
    assert clock.sleeps == [2]
    assert all(method == "GET" for method, _, _ in api.calls)
    assert api.calls[0] == ("GET", "/api/v1/plugins/_/installations", None)


@pytest.mark.parametrize("missing", ["all", "langbot-team/AIImagePlugin"])
def test_persisted_packages_missing_from_runtime_are_waited_for_without_install(
    tmp_path, missing,
):
    api = restarting_installer(package_release(tmp_path))
    ready = deepcopy(api.plugins)
    for plugin in ready:
        plugin["status"] = "initialized"
    api.plugins = [
        plugin for plugin in ready
        if missing != "all" and setup.plugin_identity(plugin) != missing
    ]
    clock = ReadinessClock()

    def recover(delay):
        clock.sleep(delay)
        api.plugins = deepcopy(ready)

    setup.ensure_plugins(api, tmp_path, sleep=recover)
    assert clock.sleeps == [2]
    assert api.calls[0] == ("GET", "/api/v1/plugins/_/installations", None)
    assert all(method == "GET" for method, _, _ in api.calls)


def test_persisted_digest_takes_precedence_over_runtime_package_metadata(tmp_path):
    api = restarting_installer(package_release(tmp_path))
    for plugin in api.plugins:
        plugin["status"] = "initialized"
        plugin["install_info"]["_certification"]["artifact_digest"] = "f" * 64
    setup.ensure_plugins(api, tmp_path, sleep=lambda _: pytest.fail("Already ready"))
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize("reason", ["missing", "changed"])
def test_only_missing_or_changed_persisted_package_is_installed(tmp_path, reason):
    packages = package_release(tmp_path)
    api = PackageInstaller(packages)
    setup.ensure_plugins(api, tmp_path, sleep=lambda _: None)
    api.installed.clear()
    selected = "langbot-team/AIImagePlugin"
    if reason == "missing":
        api.installations = [
            item for item in api.installations if item["plugin_name"] != "AIImagePlugin"
        ]
    else:
        next(
            item for item in api.installations if item["plugin_name"] == "AIImagePlugin"
        )["artifact_digest"] = "0" * 64
    # Runtime still advertises the release digest. It cannot override the DB.
    setup.ensure_plugins(api, tmp_path, sleep=lambda _: None)
    assert api.installed == [selected]
    setup.ensure_plugins(api, tmp_path, sleep=lambda _: None)
    assert api.installed == [selected]


def test_finished_install_task_still_waits_for_worker_initialization(tmp_path):
    api = PackageInstaller(package_release(tmp_path))
    install = api.install
    sleeps = []

    def starting_install(path):
        task_id = install(path)
        api.plugins[-1]["status"] = "starting"
        return task_id

    def recover(delay):
        sleeps.append(delay)
        api.plugins[-1]["status"] = "initialized"

    api.install = starting_install
    setup.ensure_plugins(api, tmp_path, sleep=recover)
    assert len(api.installed) == 7
    assert sleeps == [2] * 7


@pytest.mark.parametrize("digest_changed", [False, True])
def test_disabled_persisted_package_stops_before_any_install(tmp_path, digest_changed):
    packages = package_release(tmp_path)
    api = PackageInstaller(packages)
    # Other packages are absent, so fail before the first potentially valid install.
    item = next(p for p in packages if p["identity"] == "langbot-team/AIImagePlugin")
    record = persistent_installation(item, enabled=False)
    if digest_changed:
        record["artifact_digest"] = "0" * 64
    api.installations = [record]
    with pytest.raises(SetupError, match="插件已禁用"):
        setup.ensure_plugins(api, tmp_path)
    assert api.installed == []
    assert api.calls == [("GET", "/api/v1/plugins/_/installations", None)]
    assert api.installations == [record]


def test_persistent_installation_api_failure_never_falls_back_to_runtime(tmp_path):
    api = restarting_installer(package_release(tmp_path))
    calls = []

    def unavailable(method, path, data=None):
        calls.append((method, path))
        assert path == "/api/v1/plugins/_/installations"
        raise SetupError("Persistent API unavailable")

    api.call = unavailable
    with pytest.raises(SetupError, match="Persistent API unavailable"):
        setup.ensure_plugins(api, tmp_path)
    assert calls == [("GET", "/api/v1/plugins/_/installations")]


@pytest.mark.parametrize("invalid", ["missing", "null", "duplicate", "digest", "enabled"])
def test_invalid_persistent_installations_never_authorize_reinstallation(
    tmp_path, invalid,
):
    packages = package_release(tmp_path)
    api = restarting_installer(packages)
    records = [persistent_installation(item) for item in packages]
    response = {"installations": records}
    if invalid == "missing":
        response = {}
    elif invalid == "null":
        response["installations"] = None
    elif invalid == "duplicate":
        records.append(deepcopy(records[0]))
    elif invalid == "digest":
        records[0]["artifact_digest"] = None
    else:
        records[0]["enabled"] = "false"
    calls = []

    def malformed(method, path, data=None):
        calls.append((method, path))
        assert path == "/api/v1/plugins/_/installations"
        return response

    api.call = malformed
    with pytest.raises(SetupError, match="持久插件安装记录格式无效"):
        setup.ensure_plugins(api, tmp_path)
    assert calls == [("GET", "/api/v1/plugins/_/installations")]


@pytest.mark.parametrize("state", ["failed", "starting"])
def test_matching_package_failure_or_timeout_never_falls_back_to_install(
    tmp_path, monkeypatch, state,
):
    api = restarting_installer(package_release(tmp_path))
    for plugin in api.plugins:
        plugin["status"] = state
    clock = ReadinessClock()
    waiter = setup.wait_for_plugins_initialized
    monkeypatch.setattr(
        setup, "wait_for_plugins_initialized",
        lambda api, **kwargs: waiter(
            api, **{**kwargs, "timeout": 3, "clock": clock.monotonic},
        ),
    )
    with pytest.raises(SetupError, match="启动失败" if state == "failed" else "启动超时"):
        setup.ensure_plugins(api, tmp_path, sleep=clock.sleep)
    assert clock.sleeps == ([] if state == "failed" else [2, 1])
    assert all(method == "GET" for method, _, _ in api.calls)
    assert api.calls[0] == ("GET", "/api/v1/plugins/_/installations", None)


def test_single_plugin_wait_does_not_require_unrelated_plugins_to_be_ready():
    api = FakeLangBot()
    selected = api.plugins[0]
    identity = setup.plugin_identity(selected)
    for plugin in api.plugins[1:]:
        plugin["status"] = "starting"
    setup.wait_for_plugins_initialized(api, identities={identity})
    assert api.calls == [("GET", "/api/v1/plugins", None)]


def test_bad_release_hash_is_rejected_before_any_install(tmp_path):
    packages = package_release(tmp_path)
    (tmp_path / packages[-1]["filename"]).write_bytes(b"synthetic-corrupt")

    class NoCalls:
        def call(self, *args):
            pytest.fail("The complete release must be verified before API use")

        def install(self, *args):
            pytest.fail("An invalid release must not partially install")

    with pytest.raises(SetupError, match="校验"):
        setup.ensure_plugins(NoCalls(), tmp_path)
