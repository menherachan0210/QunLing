"""Check a fresh CI deployment with synthetic settings and no model calls or QQ login.

Run inside the wizard container. --configure exercises actual provisioning with
fake model names/keys and a closed loopback port, then inspects resource links.
No model/chat endpoint is invoked. No tokens, QR codes or API responses are
printed; restart snapshots contain hashes, package identities and resource IDs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bootstrap.api import LangBotAPI, NapCatAPI, SetupError  # noqa: E402
from bootstrap.configure import PLUGINS, RUNNER, plugin_identity  # noqa: E402
from bootstrap.state import State  # noqa: E402
from scripts.chat_style import styled_prompts  # noqa: E402


class SmokeError(RuntimeError):
    pass


SYNTHETIC_AI = {
    "base_url": "http://127.0.0.1:1/v1",
    "api_key": "offline-ci-no-real-api-key",
    "chat_model": "offline-ci-chat",
    "vision_model": "offline-ci-vision",
    "image_model": "offline-ci-image",
    "search_model": "offline-ci-search",
}


def fetch_json(url, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    opener = build_opener(ProxyHandler({}))
    with opener.open(Request(url, headers=headers), timeout=10) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
        if response.status != 200 or len(raw) > 2 * 1024 * 1024:
            raise SmokeError("Unexpected management HTTP response")
        result = json.loads(raw)
    if not isinstance(result, dict):
        raise SmokeError("Unexpected management JSON shape")
    return result


def await_ready(state, timeout, *, configured=False):
    """Wait for installed plugins and the expected phase; QQ must stay logged out."""
    values = state.read("secrets.json", {})
    if not all(
        values.get(key) for key in ("setup_token", "napcat_token", "onebot_token")
    ):
        raise SmokeError("Initialization credentials are missing")
    napcat = NapCatAPI(os.environ["QQBOT_NAPCAT_URL"], values["napcat_token"])
    deadline = time.monotonic() + timeout
    stage = "starting"
    while time.monotonic() < deadline:
        saved_ai = state.read("ai.json")
        if saved_ai is not None and (not configured or saved_ai != SYNTHETIC_AI):
            raise SmokeError(
                "Smoke checks refuse existing or non-synthetic AI settings"
            )
        try:
            status = fetch_json(
                "http://127.0.0.1:5310/api/status", values["setup_token"]
            )
            stage = status.get("stage")
            if stage == "error":
                raise SmokeError("Wizard initialization failed; inspect it locally")
            if status.get("qq_logged_in") or status.get("qq_connected"):
                raise SmokeError("Smoke checks must not log into QQ")
            if not configured and status.get("configured"):
                raise SmokeError(
                    "Smoke checks require an unconfigured fresh deployment"
                )
            ready_stage = "waiting_qq" if configured else "waiting_ai"
            if (
                status.get("plugins_ready") is True
                and stage == ready_stage
                and bool(status.get("configured")) is configured
            ):
                login = napcat.call("/QQLogin/CheckLoginStatus")
                if login.get("isLogin") is True:
                    raise SmokeError("Smoke checks require QQ to remain logged out")
                if login.get("isLogin") is False:
                    return
        except (URLError, OSError, ValueError, SetupError):
            # Service readiness may lag behind the HTTP liveness check.
            pass
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    # Do not include server-controlled status messages or login QR content.
    safe_stage = (
        stage
        if stage in {"starting", "waiting_ai", "configuring", "waiting_qq"}
        else "unknown"
    )
    raise SmokeError(f"Fresh initialization timed out (stage={safe_stage})")


def submit_synthetic_config(state):
    """Submit exactly once through the real wizard HTTP boundary, never a model API."""
    url = "http://127.0.0.1:5310"
    token = state.read("secrets.json")["setup_token"]
    request = Request(
        url + "/api/configure",
        data=json.dumps(SYNTHETIC_AI).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "Origin": url,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    opener = build_opener(ProxyHandler({}))
    with opener.open(request, timeout=15) as response:
        if response.status != 202:
            raise SmokeError("Wizard did not accept synthetic configuration")


def inspect_configuration(state, api):
    """Validate real API configuration contracts, and record IDs to detect duplicates."""
    record = state.read(default={})
    if not record.get("configured") or state.read("ai.json") != SYNTHETIC_AI:
        raise SmokeError("Synthetic provisioning is incomplete")
    collections = {
        "providers": ("/api/v1/provider/providers", "providers"),
        "llms": ("/api/v1/provider/models/llm", "models"),
        "embeddings": ("/api/v1/provider/models/embedding", "models"),
        "knowledge": ("/api/v1/knowledge/bases", "bases"),
        "pipelines": ("/api/v1/pipelines", "pipelines"),
        "agents": ("/api/v1/agents", "agents"),
        "bots": ("/api/v1/platform/bots", "bots"),
    }
    rows = {
        key: api.call("GET", url)[field] for key, (url, field) in collections.items()
    }

    def require(condition, label):
        if not condition:
            raise SmokeError("Configuration contract failed: " + label)

    def managed(kind, key):
        found = [row for row in rows[kind] if row["uuid"] == record.get(key)]
        require(len(found) == 1, key)
        return found[0]

    provider = managed("providers", "provider_uuid")
    require(
        provider["requester"] == "openai-chat-completions"
        and provider["base_url"] == SYNTHETIC_AI["base_url"],
        "chat provider",
    )
    chat = managed("llms", "chat_model_uuid")
    vision = managed("llms", "vision_model_uuid")
    for role, model in (("chat_model", chat), ("vision_model", vision)):
        require(
            model["name"] == SYNTHETIC_AI[role]
            and model["provider_uuid"] == provider["uuid"]
            and {"func_call", "vision"}.issubset(model.get("abilities", [])),
            role,
        )
    require(chat["uuid"] != vision["uuid"], "distinct model roles")
    local = managed("providers", "embedding_provider_uuid")
    embedding = managed("embeddings", "embedding_uuid")
    require(
        local["requester"] == "chroma-embedding"
        and embedding["provider_uuid"] == local["uuid"],
        "local embedding",
    )
    memory = managed("knowledge", "memory_uuid")
    require(
        memory["knowledge_engine_plugin_id"] == "langbot-team/LongTermMemory"
        and memory["creation_settings"]["embedding_model_uuid"] == embedding["uuid"]
        and memory["creation_settings"]["isolation"] == "session",
        "memory isolation",
    )
    managed("pipelines", "pipeline_uuid")
    pipeline = api.call("GET", "/api/v1/pipelines/" + record["pipeline_uuid"])[
        "pipeline"
    ]
    config = pipeline["config"]
    require(config["ai"]["runner"]["id"] == RUNNER, "LocalAgent runner")
    runner = config["ai"]["runner_config"][RUNNER]
    require(
        runner["model"]["primary"] == chat["uuid"]
        and runner["knowledge-bases"].count(memory["uuid"]) == 1
        and runner["box-enabled"] is False
        and runner["max-tool-iterations"] >= 12,
        "runner bindings",
    )
    require(
        config["trigger"]["group-respond-rules"]
        == {
            "at": True,
            "prefix": ["/ai"],
            "regexp": [],
            "random": 0.0,
        },
        "group trigger",
    )
    require(
        config["output"]["misc"]["quote-origin"] is True
        and pipeline["extensions_preferences"]["enable_all_plugins"] is True,
        "pipeline output and plugin enablement",
    )
    managed("bots", "bot_uuid")
    bot = api.call("GET", "/api/v1/platform/bots/" + record["bot_uuid"])["bot"]
    require(
        bot["adapter"] == "onebot-v11"
        and bot["enable"] is True
        and bot["adapter_config"]["connection_mode"] == "reverse_ws",
        "OneBot adapter",
    )
    require(
        any(
            binding.get("target_uuid") == record["pipeline_uuid"]
            and binding.get("target_type") == "pipeline"
            and binding.get("enabled") is True
            for binding in bot["event_bindings"]
        ),
        "bot pipeline",
    )
    for name in ("QQTextReader", "QQComicTranslator"):
        processor = managed("agents", name + "_processor_uuid")
        component = "plugin:local/" + name + "/group_upload"
        require(
            processor["kind"] == "event_processor"
            and processor["component_ref"] == component,
            name + " processor",
        )
        require(
            sum(row.get("component_ref") == component for row in rows["agents"]) == 1,
            name + " duplicate processor",
        )
        require(
            sum(
                p["processor_uuid"] == processor["uuid"] and p["enabled"]
                for p in bot["plugin_processors"]
            )
            == 1,
            name + " bot binding",
        )
    desired = {
        "langbot-team/AIImagePlugin": {
            "api_base_url": SYNTHETIC_AI["base_url"],
            "model_name": SYNTHETIC_AI["image_model"],
        },
        "local/QQWebSearch": {
            "api_base_url": SYNTHETIC_AI["base_url"],
            "model_name": SYNTHETIC_AI["search_model"],
            "vision_model_uuid": vision["uuid"],
        },
        "local/QQGroupContext": {"model_uuid": vision["uuid"]},
        "local/QQComicTranslator": {
            "model_uuid": vision["uuid"],
            "seven_zip_path": os.environ["QQBOT_ARCHIVE_PATH"],
        },
    }
    for identity, expected in desired.items():
        actual = api.call("GET", "/api/v1/plugins/" + identity + "/config")["config"]
        require(
            all(actual.get(key) == value for key, value in expected.items()),
            identity + " parameters",
        )
    return {kind: sorted(row["uuid"] for row in items) for kind, items in rows.items()}


def snapshot(state, packages_dir, api, *, configured=False):
    """Inspect installed packages and hash private state without exposing it."""
    index = json.loads((packages_dir / "packages.json").read_text(encoding="utf-8"))
    if isinstance(index, dict):
        index = index.get("packages", [])
    if len(index) != len(PLUGINS) or {item["identity"] for item in index} != PLUGINS:
        raise SmokeError("The image does not contain all seven plugin packages")
    expected = {}
    for item in index:
        package = packages_dir / item["filename"]
        if package.parent.resolve() != packages_dir.resolve():
            raise SmokeError("Package index points outside its directory")
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            raise SmokeError("A bundled plugin package has changed")
        expected[item["identity"]] = digest
    plugins = api.call("GET", "/api/v1/plugins")["plugins"]
    actual = {}
    for plugin in plugins:
        identity = plugin_identity(plugin)
        if identity in PLUGINS:
            if identity in actual or plugin.get("status") != "initialized":
                raise SmokeError("Required plugins are duplicated or not initialized")
            actual[identity] = (
                plugin.get("install_info", {})
                .get("_certification", {})
                .get("artifact_digest")
            )
    if actual != expected:
        raise SmokeError("Installed plugins do not match the image package hashes")
    credentials = {}
    for name in ("secrets.json", "langbot-admin.json"):
        content = state.read(name)
        if not isinstance(content, dict) or not content:
            raise SmokeError("Persistent initialization credentials are missing")
        credentials[name] = hashlib.sha256(
            json.dumps(content, sort_keys=True).encode()
        ).hexdigest()
    if not configured and state.read("ai.json") is not None:
        raise SmokeError("Smoke checks unexpectedly encountered saved AI settings")
    # Exercising the function catches omitted config/chat-style.txt in the image.
    if len(styled_prompts([])) != 1:
        raise SmokeError("Bundled conversation style is unavailable")
    result = {"credentials": credentials, "plugins": actual}
    if configured:
        result["resources"] = inspect_configuration(state, api)
        result["ai_sha256"] = hashlib.sha256(
            json.dumps(state.read("ai.json"), sort_keys=True).encode()
        ).hexdigest()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument(
        "--configure",
        action="store_true",
        help="Provision synthetic models through the actual wizard API",
    )
    snapshots = parser.add_mutually_exclusive_group()
    snapshots.add_argument("--save", type=Path)
    snapshots.add_argument("--compare", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 2400:
        parser.error("--timeout must be between 1 and 2400 seconds")
    if args.configure and args.compare:
        parser.error(
            "--compare inspects the restarted configuration without submitting it"
        )
    os.umask(0o077)
    state = State(os.environ["QQBOT_STATE_DIR"])
    if args.configure:
        await_ready(state, args.timeout)
        submit_synthetic_config(state)
    configured = args.configure or bool(args.compare)
    await_ready(state, args.timeout, configured=configured)
    api = LangBotAPI(os.environ["QQBOT_LANGBOT_URL"], state)
    api.login()
    current = snapshot(
        state, Path(os.environ["QQBOT_PACKAGES_DIR"]), api, configured=configured
    )
    if args.compare:
        previous = json.loads(args.compare.read_text(encoding="utf-8"))
        if previous != current:
            raise SmokeError("Credentials or installed packages changed after restart")
    if args.save:
        args.save.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")
    print(
        "PASS: seven initialized plugins; "
        + ("synthetic configuration verified; " if configured else "AI unconfigured; ")
        + "QQ logged out; "
        + ("restart state preserved." if args.compare else "fresh state recorded.")
    )


if __name__ == "__main__":
    try:
        main()
    except SmokeError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        # A raw HTTP/JSON exception can contain private management credentials.
        raise SystemExit(
            "Smoke check failed while inspecting deployment state"
        ) from None
