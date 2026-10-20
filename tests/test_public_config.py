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

    def call(self, method, path, data=None):
        self.calls.append((method, path, deepcopy(data)))
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
    }
    assert public["api_key_saved"] is True
    assert "synthetic-private" not in json.dumps(public)
    assert config["api_key"] not in json.dumps(public)


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


def test_install_seven_packages_localagent_first_and_skip_matching_initialized(tmp_path):
    packages = package_release(tmp_path)

    class Installer:
        def __init__(self):
            self.plugins = []
            self.installed = []

        def call(self, method, path, data=None):
            assert method == "GET"
            if path == "/api/v1/plugins":
                return {"plugins": deepcopy(self.plugins)}
            assert path.startswith("/api/v1/system/tasks/")
            return {"runtime": {"state": "FINISHED", "exception": None}}

        def install(self, path):
            item = next(p for p in packages if p["filename"] == path.name)
            author, name = item["identity"].split("/")
            self.installed.append(item["identity"])
            self.plugins.append({
                "manifest": {"manifest": {"metadata": {"author": author, "name": name}}},
                "status": "initialized",
                "install_info": {"_certification": {"artifact_digest": item["sha256"]}},
            })
            return len(self.installed)

    api = Installer()
    setup.ensure_plugins(api, tmp_path, sleep=lambda _: None)
    assert api.installed[0] == "langbot-team/LocalAgent"
    assert set(api.installed) == setup.PLUGINS and len(api.installed) == 7
    setup.ensure_plugins(api, tmp_path, sleep=lambda _: None)
    assert len(api.installed) == 7


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
