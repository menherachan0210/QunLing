from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy/compose"


def load(name):
    spec = importlib.util.spec_from_file_location(
        "public_compose_" + name, COMPOSE / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load("prepare")
entrypoint = load("entrypoint")
healthcheck = load("healthcheck")
smoke = load("smoke")


def make_zip(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, body in entries:
            if isinstance(name, str):
                entry = zipfile.ZipInfo("placeholder")
                # ZipInfo's Windows constructor normalizes backslashes. Set the
                # archive header name directly to exercise malformed raw ZIPs.
                entry.filename = name
                entry.orig_filename = name
            else:
                entry = name
            archive.writestr(entry, body)
    return output.getvalue()


def test_compose_has_only_loopback_admin_ports_and_no_runtime_exposure():
    document = yaml.safe_load((COMPOSE / "compose.yaml").read_text(encoding="utf-8"))
    services = document["services"]
    assert set(services) == {"init", "app", "wizard", "napcat", "updater"}
    published = [
        port for service in services.values() for port in service.get("ports", [])
    ]
    assert sorted(published) == [
        "127.0.0.1:5300:5300",
        "127.0.0.1:5310:5310",
        "127.0.0.1:6099:6099",
    ]
    for name, service in services.items():
        assert service.get("privileged") is not True
        assert service.get("network_mode") != "host"
        sockets = [
            volume
            for volume in service.get("volumes", [])
            if "docker.sock" in str(volume)
        ]
        assert sockets == (
            ["/var/run/docker.sock:/var/run/docker.sock"] if name == "updater" else []
        )
        assert service["platform"] == "linux/amd64"
    assert not services["updater"].get("ports")
    assert "setup-state:/var/lib/qqbot-setup:ro" in services["updater"]["volumes"]
    assert "update-data:/var/lib/qqbot-updater" in services["updater"]["volumes"]
    assert "update-data" in document["volumes"]
    assert services["app"]["command"] == ["app"]
    assert services["app"]["environment"]["BOX__ENABLED"] == "false"
    assert services["app"]["environment"]["LANGBOT_PLUGIN_RUNTIME_TRANSPORT"] == "stdio"
    assert services["app"]["volumes"] == ["langbot-data:/opt/qq-ai-bot/langbot/data"]
    assert services["wizard"]["environment"]["QQBOT_LANGBOT_URL"] == "http://app:5300"
    assert services["wizard"]["environment"]["QQBOT_NAPCAT_URL"] == "http://napcat:6099"
    assert services["wizard"]["depends_on"]["app"]["condition"] == "service_healthy"
    for name in ("app", "wizard", "napcat"):
        assert (
            services[name]["depends_on"]["init"]["condition"]
            == "service_completed_successfully"
        )


def test_napcat_image_and_source_digests_match_recorded_pins():
    pins = json.loads((COMPOSE / "sources.lock.json").read_text(encoding="utf-8"))
    compose = yaml.safe_load((COMPOSE / "compose.yaml").read_text(encoding="utf-8"))
    assert compose["services"]["napcat"]["image"] == pins["napcat"]["image"]
    assert "@sha256:" in pins["napcat"]["image"]
    assert pins["napcat"]["version"] != "latest"
    assert pins["python"]["image"] in (COMPOSE / "Dockerfile").read_text()
    assert pins["langbot"]["version"] == "4.11.0"
    assert len(pins["langbot"]["sha256"]) == 64
    assert pins["upstream_plugins"]["commit"] in pins["upstream_plugins"]["url"]
    assert pins["upstream_plugins"]["local_agent_version"] == "0.2.1"


def test_explicit_docker_copies_never_import_workstation_runtimes():
    dockerfile = (COMPOSE / "Dockerfile").read_text()
    copies = [
        line.split()[1:-1]
        for line in dockerfile.splitlines()
        if line.startswith("COPY ")
    ]
    sources = {source for group in copies for source in group}
    assert sources == {
        "deploy/compose",
        "deploy/compose/prepare.py",
        "deploy/compose/sources.lock.json",
        "scripts/Apply-RuntimePatches.py",
        "scripts/Setup-ArchiveTools.py",
        "scripts/Setup-FileRuntime.py",
        "scripts/Run-LangBot.py",
        "scripts/chat_style.py",
        "qunling",
        "qunling/requirements.txt",
        "LICENSE",
        "NOTICE",
        "THIRD_PARTY_NOTICES.md",
        "licenses",
        "config/chat-style.txt",
        "patches",
        "bootstrap",
        *(
            "custom-plugins/" + name
            for name in prepare.PLUGIN_NAMES
            if name != "LocalAgent"
        ),
    }
    assert all(
        not source.startswith((".local", "langbot", "napcat", "downloads"))
        for source in sources
    )
    ignore = (COMPOSE / "Dockerfile.dockerignore").read_text().splitlines()
    assert ignore[0] == "**"
    assert "**/.env" in ignore and "**/data/" in ignore and "**/*.lbpkg" in ignore
    assert "!config/" in ignore and "!config/chat-style.txt" in ignore
    assert all(
        "!" + name in ignore for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md")
    )
    assert "!licenses/**" in ignore
    assert not any(
        line.startswith(("!langbot", "!.local", "!napcat", "!downloads"))
        for line in ignore
    )


def test_verified_download_checks_exact_size_and_digest_before_returning():
    raw = b"public release"
    asset = {
        "url": "https://example.test/release.zip",
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    opener = Mock(return_value=io.BytesIO(raw))
    assert prepare.download_verified(asset, opener=opener) == raw
    assert opener.call_args.kwargs["timeout"] == 60
    for bad in (raw[:-1], raw + b"extra", b"x" * len(raw)):
        with pytest.raises(ValueError, match="SHA-256"):
            prepare.download_verified(asset, opener=Mock(return_value=io.BytesIO(bad)))


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "C:/absolute", "a\\b", "a/../escape"]
)
def test_archive_path_errors_are_rejected_before_any_write(tmp_path, name):
    target = tmp_path / "source"
    raw = make_zip([("normal.txt", b"ok"), (name, b"bad")])
    with pytest.raises(ValueError, match="Unsafe"):
        prepare.extract_tree(raw, target)
    assert not target.exists()
    assert not (tmp_path / "escape").exists()


def test_archive_symlinks_and_duplicate_names_are_rejected(tmp_path):
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError, match="Unsafe"):
        prepare.extract_tree(
            make_zip([(link, b"../outside")]), tmp_path / "link-source"
        )
    with pytest.warns(UserWarning, match="Duplicate"):
        raw = make_zip([("same", b"a"), ("same", b"b")])
    with pytest.raises(ValueError, match="Duplicate"):
        prepare.extract_tree(raw, tmp_path / "duplicate-source")


def source_link(name):
    entry = zipfile.ZipInfo(name)
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    return entry


def test_pinned_sdk_document_alias_is_materialized_as_an_ordinary_file(tmp_path):
    raw = make_zip(
        [
            ("sdk/AGENTS.md", b"Upstream documentation\n"),
            (source_link("sdk/CLAUDE.md"), b"AGENTS.md"),
        ]
    )
    destination = tmp_path / "sdk"
    assert (
        prepare.extract_tree(
            raw,
            destination,
            prefix="sdk/",
            document_links={"CLAUDE.md": "AGENTS.md"},
        )
        == 2
    )
    assert (destination / "CLAUDE.md").read_bytes() == (
        destination / "AGENTS.md"
    ).read_bytes()
    assert not (destination / "CLAUDE.md").is_symlink()
    assert stat.S_ISREG((destination / "CLAUDE.md").stat().st_mode)


@pytest.mark.parametrize(
    "link_target, mapping, target_name, target_kind",
    [
        (b"../outside", {"CLAUDE.md": "AGENTS.md"}, "AGENTS.md", "file"),
        (b"../outside", {"CLAUDE.md": "../outside"}, "../outside", "file"),
        (b"/absolute", {"CLAUDE.md": "/absolute"}, "/absolute", "file"),
        (b"OTHER.md", {"CLAUDE.md": "AGENTS.md"}, "AGENTS.md", "file"),
        (b"AGENTS.md", {"CLAUDE.md": "AGENTS.md"}, "AGENTS.md", "link"),
        (b"AGENTS.md", {"CLAUDE.md": "AGENTS.md"}, "missing", "file"),
        (b"AGENTS.md", {}, "AGENTS.md", "file"),
    ],
)
def test_document_alias_policy_rejects_escape_changed_or_indirect_targets_before_write(
    tmp_path, link_target, mapping, target_name, target_kind
):
    target = (
        source_link("sdk/" + target_name)
        if target_kind == "link"
        else "sdk/" + target_name
    )
    raw = make_zip(
        [
            (source_link("sdk/CLAUDE.md"), link_target),
            (target, b"target contents"),
        ]
    )
    destination = tmp_path / "sdk"
    with pytest.raises(ValueError):
        prepare.extract_tree(raw, destination, prefix="sdk/", document_links=mapping)
    assert not destination.exists()


def test_pinned_document_allowance_does_not_allow_another_archive_link(tmp_path):
    raw = make_zip(
        [
            ("sdk/AGENTS.md", b"upstream"),
            (source_link("sdk/CLAUDE.md"), b"AGENTS.md"),
            (source_link("sdk/other.md"), b"AGENTS.md"),
        ]
    )
    destination = tmp_path / "sdk"
    with pytest.raises(ValueError, match="Unsafe"):
        prepare.extract_tree(
            raw,
            destination,
            prefix="sdk/",
            document_links={"CLAUDE.md": "AGENTS.md"},
        )
    assert not destination.exists()


def test_public_source_extraction_only_selects_pinned_subtree(tmp_path):
    raw = make_zip(
        [
            ("repo/Runner/LocalAgent/main.py", b"pass\n"),
            ("repo/Runner/LocalAgent/assets/icon.svg", b"<svg/>"),
            ("repo/Runner/LocalAgent/.git/config", b"git metadata"),
            ("repo/Other/main.py", b"other plugin"),
        ]
    )
    destination = tmp_path / "LocalAgent"
    assert prepare.extract_tree(raw, destination, prefix="repo/Runner/LocalAgent/") == 2
    assert sorted(
        str(path.relative_to(destination)).replace("\\", "/")
        for path in destination.rglob("*")
        if path.is_file()
    ) == ["assets/icon.svg", "main.py"]
    with pytest.raises(ValueError, match="overwrite"):
        prepare.extract_tree(raw, destination, prefix="repo/Runner/LocalAgent/")
    assert (destination / "main.py").read_bytes() == b"pass\n"


def test_prepared_image_retains_complete_runtime_source_and_upstream_licenses(
    tmp_path, monkeypatch
):
    pins = json.loads(prepare.LOCK.read_text(encoding="utf-8"))
    plugin_prefix = pins["upstream_plugins"]["local_agent_prefix"]
    plugin_repo = plugin_prefix.split("/")[0] + "/"
    sdk_prefix = pins["plugin_sdk_source"]["prefix"]
    archives = {
        pins["langbot"]["url"]: make_zip(
            [
                ("web/dist/index.html", b"<html>WebUI</html>"),
                ("LICENSE", b"upstream host license"),
            ]
        ),
        pins["upstream_plugins"]["url"]: make_zip(
            [
                (plugin_prefix + "manifest.yaml", b"metadata: {}"),
                (plugin_repo + "LICENSE", b"upstream plugin license"),
            ]
        ),
        pins["plugin_sdk_source"]["url"]: make_zip(
            [
                (sdk_prefix + "LICENSE", b"SDK license"),
                (sdk_prefix + "README.md", b"License scopes"),
                (sdk_prefix + "pyproject.toml", b"[project]\nversion='0.7.11'\n"),
                (sdk_prefix + "AGENTS.md", b"Upstream source documentation"),
                (source_link(sdk_prefix + "CLAUDE.md"), b"AGENTS.md"),
                (sdk_prefix + "src/langbot_plugin/runtime/LICENSE", b"Runtime license"),
                (
                    sdk_prefix + "src/langbot_plugin/runtime/main.py",
                    b"# upstream source\n",
                ),
                (sdk_prefix + "tests/test_runtime.py", b"# upstream tests\n"),
            ]
        ),
    }
    downloaded = []

    def download(asset):
        downloaded.append(asset["url"])
        return archives[asset["url"]]

    monkeypatch.setattr(prepare, "download_verified", download)
    root = tmp_path / "image"
    prepare.prepare_sources(root)
    sdk = root / "upstream/langbot-plugin-sdk"
    assert (
        sdk / "src/langbot_plugin/runtime/main.py"
    ).read_bytes() == b"# upstream source\n"
    assert (sdk / "tests/test_runtime.py").is_file()
    assert (
        sdk / "src/langbot_plugin/runtime/LICENSE"
    ).read_bytes() == b"Runtime license"
    assert (sdk / "LICENSE").read_bytes() == b"SDK license"
    assert (sdk / "CLAUDE.md").read_bytes() == (sdk / "AGENTS.md").read_bytes()
    assert not (sdk / "CLAUDE.md").is_symlink()
    assert (root / "custom-plugins/LocalAgent/UPSTREAM_LICENSE").is_file()
    assert len(downloaded) == 3 and set(downloaded) == set(archives)


def test_package_validation_rejects_cached_data_or_credentials(tmp_path):
    metadata = yaml.safe_dump(
        {"metadata": {"author": "local", "name": "Example", "version": "1.0"}}
    )
    for forbidden in (
        "data/private.json",
        ".env",
        ".env.local",
        "dist/old.lbpkg",
        "__pycache__/module.pyc",
    ):
        package = tmp_path / "example.lbpkg"
        package.write_bytes(
            make_zip([("manifest.yaml", metadata), (forbidden, b"private")])
        )
        with pytest.raises(ValueError, match="excluded"):
            prepare.package_metadata(package)


def test_build_packages_requires_all_seven_and_emits_verifiable_index(
    tmp_path, monkeypatch
):
    for name in prepare.PLUGIN_NAMES:
        source = tmp_path / "custom-plugins" / name
        source.mkdir(parents=True)
        (source / "manifest.yaml").write_text("source", encoding="utf-8")
    calls = []
    (tmp_path / "qunling").mkdir()
    (tmp_path / "qunling/__init__.py").write_text('"""Synthetic public source."""')
    prepare.os.utime(tmp_path / "qunling/__init__.py", (0, 0))
    for notice in ("LICENSE", "NOTICE"):
        (tmp_path / notice).write_text("Synthetic license metadata")
    patch_plugin = Mock()
    monkeypatch.setattr(prepare, "apply_local_agent_patch", patch_plugin)

    def build(command, *, cwd, check):
        assert check is True
        assert command[:4] == [sys.executable, "-m", "langbot_plugin.cli", "build"]
        name = cwd.name
        assert (cwd / "qunling/__init__.py").is_file()
        assert (cwd / "qunling/LICENSE").is_file()
        assert (cwd / "qunling/NOTICE").is_file()
        assert (cwd / "qunling/__init__.py").stat().st_mtime >= 315532800
        calls.append(name)
        metadata = {
            "metadata": {
                "author": "local",
                "name": name,
                "version": "0.2.1" if name == "LocalAgent" else "1.0",
            }
        }
        target = Path(command[-1]) / (name + ".lbpkg")
        target.write_bytes(
            make_zip(
                [("manifest.yaml", yaml.safe_dump(metadata)), ("main.py", b"pass\n")]
                + [(p.relative_to(cwd).as_posix(), p.read_bytes())
                   for p in (cwd / "qunling").rglob("*") if p.is_file()]
            )
        )

    monkeypatch.setattr(prepare.subprocess, "run", build)
    prepare.build_packages(tmp_path)
    index = json.loads((tmp_path / "packages/packages.json").read_text())
    assert calls == list(prepare.PLUGIN_NAMES)
    assert len(index) == 7
    patch_plugin.assert_called_once()
    for item in index:
        package = tmp_path / "packages" / item["filename"]
        assert hashlib.sha256(package.read_bytes()).hexdigest() == item["sha256"]
    with pytest.raises(ValueError, match="stale"):
        prepare.build_packages(tmp_path)


@pytest.mark.parametrize("payload", [None, b"stale tool code"])
def test_package_validation_rejects_silently_dropped_or_changed_tool(tmp_path, payload):
    source = tmp_path / "source"
    tool = source / "components/tools/qq_action.py"
    tool.parent.mkdir(parents=True)
    tool.write_bytes(b"current tool code")
    metadata = yaml.safe_dump(
        {"metadata": {"author": "local", "name": "Example", "version": "1.0"}}
    )
    entries = [("manifest.yaml", metadata)]
    if payload is not None:
        entries.append(("components/tools/qq_action.py", payload))
    package = tmp_path / "example.lbpkg"
    package.write_bytes(make_zip(entries))
    with pytest.raises(ValueError, match="source missing or changed"):
        prepare.package_metadata(package, source=source)


def test_entrypoint_init_calls_idempotent_state_preparer_only(
    tmp_path, monkeypatch, capsys
):
    state, config = tmp_path / "state", tmp_path / "napcat"
    callback = Mock()
    monkeypatch.setitem(
        sys.modules, "bootstrap.state", SimpleNamespace(prepare=callback)
    )
    monkeypatch.setattr(entrypoint, "ROOT", tmp_path)
    monkeypatch.setattr(entrypoint.os, "chdir", Mock())
    monkeypatch.setattr(entrypoint.os, "umask", Mock())
    monkeypatch.setenv("QQBOT_STATE_DIR", str(state))
    monkeypatch.setenv("QQBOT_NAPCAT_CONFIG_DIR", str(config))
    entrypoint.main(["init"])
    callback.assert_called_once_with(state, config)
    assert "token" not in capsys.readouterr().out.lower()


@pytest.mark.parametrize(
    "role,expected",
    [
        ("app", ["-u", "SUPERVISOR"]),
        ("wizard", ["-u", "-m", "bootstrap.server"]),
        ("access", ["-u", "-m", "bootstrap.server", "--url"]),
    ],
)
def test_app_and_wizard_exec_without_standalone_runtime(
    tmp_path, monkeypatch, role, expected
):
    monkeypatch.setattr(entrypoint, "ROOT", tmp_path)
    chdir, execute = Mock(), Mock()
    monkeypatch.setattr(entrypoint.os, "chdir", chdir)
    monkeypatch.setattr(entrypoint.os, "umask", Mock())

    class ReplacedProcess(Exception):
        pass

    execute.side_effect = ReplacedProcess
    monkeypatch.setattr(entrypoint.os, "execv", execute)
    with pytest.raises(ReplacedProcess):
        entrypoint.main([role])
    expected = [str(tmp_path / "scripts/Run-LangBot.py") if value == "SUPERVISOR" else value
                for value in expected]
    execute.assert_called_once_with(sys.executable, [sys.executable, *expected])
    assert chdir.call_args.args[0] == (
        tmp_path / "langbot" if role == "app" else tmp_path
    )


def test_real_init_and_access_commands_agree_with_bootstrap_and_preserve_state(
    tmp_path,
):
    private, napcat = tmp_path / "private", tmp_path / "napcat"
    environment = {
        **os.environ,
        "QQBOT_STATE_DIR": str(private),
        "QQBOT_NAPCAT_CONFIG_DIR": str(napcat),
        # Accidental manager startup must never touch a live local deployment.
        "QQBOT_LANGBOT_URL": "http://127.0.0.1:1",
        "QQBOT_NAPCAT_URL": "http://127.0.0.1:1",
    }

    def run(*arguments):
        return subprocess.run(
            [sys.executable, *arguments],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        ).stdout.strip()

    run(str(COMPOSE / "entrypoint.py"), "init")
    files = [private / "secrets.json", napcat / "webui.json", napcat / "onebot11.json"]
    first = [path.read_bytes() for path in files]
    run(str(COMPOSE / "entrypoint.py"), "init")
    assert [path.read_bytes() for path in files] == first
    token = json.loads(first[0])["setup_token"]
    expected = "http://127.0.0.1:5310/#token=" + token
    assert run(str(COMPOSE / "entrypoint.py"), "access") == expected
    assert run("-m", "bootstrap.server", "--url") == expected
    assert not (private / "langbot-admin.json").exists()
    assert not (private / "ai.json").exists()


@pytest.mark.parametrize(
    "role,status,body,healthy",
    [
        (
            "app",
            200,
            b'{"code":0,"msg":"ok","resources":{"runtimes":{"plugin_runtime_connected":true}},"plugin_runtime":{"state":"ready","healthy":true}}',
            True,
        ),
        (
            "app",
            200,
            b'{"code":0,"msg":"ok","resources":{"runtimes":{"plugin_runtime_connected":false}}}',
            False,
        ),
        (
            "app",
            200,
            b'{"code":0,"msg":"ok","resources":{"runtimes":{"plugin_runtime_connected":null}}}',
            False,
        ),
        (
            "app",
            200,
            b'{"code":0,"msg":"ok","resources":{"runtimes":{"plugin_runtime_connected":1}}}',
            False,
        ),
        ("app", 200, b'{"code":0,"msg":"ok","resources":{}}', False),
        ("app", 200, b'{"code":0,"msg":"ok"}', False),
        ("app", 200, b'{"code":0,"msg":"ok","resources":null}', False),
        ("app", 200, b'{"code":0,"msg":"ok","resources":[]}', False),
        (
            "app",
            200,
            b'{"code":0,"msg":"ok","resources":{"runtimes":null}}',
            False,
        ),
        (
            "app",
            200,
            b'{"code":0,"msg":"ok","resources":{"runtimes":[]}}',
            False,
        ),
        ("wizard", 200, b'{"status":"ok"}', True),
        ("app", 200, b'{"status":"ok"}', False),
        ("wizard", 200, b'{"code":0,"msg":"ok"}', False),
        ("app", 503, b'{"code":0,"msg":"ok"}', False),
        ("wizard", 200, b"<html>Login page</html>", False),
        ("app", 200, b"null", False),
        ("wizard", 200, b"[]", False),
    ],
)
def test_healthcheck_requires_each_services_actual_health_response(
    monkeypatch, role, status, body, healthy
):
    response = io.BytesIO(body)
    response.status = status
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(healthcheck, "build_opener", Mock(return_value=opener))
    assert healthcheck.check(role) is healthy
    expected_port = 5300 if role == "app" else 5310
    opener.open.assert_called_once_with(
        f"http://127.0.0.1:{expected_port}/healthz", timeout=3
    )


def test_healthcheck_handles_connection_failure(monkeypatch):
    opener = Mock()
    opener.open.side_effect = OSError("synthetic refused connection")
    monkeypatch.setattr(healthcheck, "build_opener", Mock(return_value=opener))
    assert not healthcheck.check("app")


def test_smoke_waits_for_both_plugins_and_logged_out_napcat_without_configuring(
    tmp_path, monkeypatch
):
    state = smoke.State(tmp_path)
    state.write(
        {
            "setup_token": "synthetic-setup",
            "napcat_token": "synthetic-napcat",
            "onebot_token": "synthetic-onebot",
        },
        "secrets.json",
    )
    monkeypatch.setenv("QQBOT_NAPCAT_URL", "http://napcat:6099")
    events = iter(
        [
            {"stage": "starting", "plugins_ready": False},
            {"stage": "waiting_ai", "plugins_ready": True},
            {"stage": "waiting_ai", "plugins_ready": True},
        ]
    )
    reads = []

    def fetch(url, token):
        reads.append((url, token))
        return next(events)

    client = Mock()
    client.call.side_effect = [smoke.SetupError("Starting"), {"isLogin": False}]
    monkeypatch.setattr(smoke, "fetch_json", fetch)
    monkeypatch.setattr(smoke, "NapCatAPI", Mock(return_value=client))
    monkeypatch.setattr(smoke.time, "sleep", Mock())
    smoke.await_ready(state, 30)
    assert len(reads) == 3
    assert all(url.endswith("/api/status") for url, _ in reads)
    assert client.call.call_args_list == [
        (("/QQLogin/CheckLoginStatus",), {}),
        (("/QQLogin/CheckLoginStatus",), {}),
    ]
    assert not (tmp_path / "ai.json").exists()


@pytest.mark.parametrize(
    "status",
    [
        {"stage": "error"},
        {"stage": "waiting_ai", "configured": True},
        {"stage": "waiting_ai", "qq_logged_in": True},
    ],
)
def test_smoke_rejects_failed_or_nonfresh_deployments(tmp_path, monkeypatch, status):
    state = smoke.State(tmp_path)
    state.write(
        {
            "setup_token": "synthetic",
            "napcat_token": "synthetic",
            "onebot_token": "synthetic",
        },
        "secrets.json",
    )
    monkeypatch.setenv("QQBOT_NAPCAT_URL", "http://napcat:6099")
    monkeypatch.setattr(smoke, "fetch_json", lambda *_: status)
    with pytest.raises(smoke.SmokeError):
        smoke.await_ready(state, 30)


def test_smoke_snapshot_verifies_actual_installed_bytes_and_hashes_credentials(
    tmp_path,
):
    private = smoke.State(tmp_path / "private")
    private.write({"setup_token": "synthetic-secret-must-not-appear"}, "secrets.json")
    private.write(
        {"password": "synthetic-password-must-not-appear"}, "langbot-admin.json"
    )
    directory = tmp_path / "packages"
    directory.mkdir()
    index, installed = [], []
    for number, identity in enumerate(sorted(smoke.PLUGINS)):
        content = f"synthetic-package-{number}".encode()
        digest = hashlib.sha256(content).hexdigest()
        filename = f"plugin-{number}.lbpkg"
        (directory / filename).write_bytes(content)
        index.append({"identity": identity, "filename": filename, "sha256": digest})
        author, name = identity.split("/")
        installed.append(
            {
                "manifest": {
                    "manifest": {"metadata": {"author": author, "name": name}}
                },
                "status": "initialized",
                "install_info": {"_certification": {"artifact_digest": digest}},
            }
        )
    (directory / "packages.json").write_text(json.dumps(index), encoding="utf-8")
    api = Mock()
    api.call.return_value = {"plugins": installed}
    recorded = smoke.snapshot(private, directory, api)
    assert len(recorded["plugins"]) == 7
    assert "synthetic-secret" not in json.dumps(recorded)
    assert "synthetic-password" not in json.dumps(recorded)
    api.call.assert_called_once_with("GET", "/api/v1/plugins")
    assert smoke.snapshot(private, directory, api) == recorded
    installed[0]["install_info"]["_certification"]["artifact_digest"] = "0" * 64
    with pytest.raises(smoke.SmokeError, match="Installed plugins"):
        smoke.snapshot(private, directory, api)
    (directory / index[0]["filename"]).write_bytes(b"corrupt package")
    with pytest.raises(smoke.SmokeError, match="bundled plugin"):
        smoke.snapshot(private, directory, api)


def test_smoke_configures_only_the_wizard_with_synthetic_closed_port_settings(
    tmp_path, monkeypatch
):
    state = smoke.State(tmp_path)
    state.write({"setup_token": "offline-token"}, "secrets.json")
    response = io.BytesIO(b"")
    response.status = 202
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(smoke, "build_opener", Mock(return_value=opener))
    smoke.submit_synthetic_config(state)
    opener.open.assert_called_once()
    request = opener.open.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:5310/api/configure"
    assert request.get_method() == "POST"
    assert request.headers["Origin"] == "http://127.0.0.1:5310"
    settings = json.loads(request.data)
    assert settings == smoke.SYNTHETIC_AI
    assert settings["base_url"] == "http://127.0.0.1:1/v1"
    assert opener.open.call_args.kwargs["timeout"] == 15


def test_configured_smoke_refuses_real_or_unknown_settings(tmp_path, monkeypatch):
    state = smoke.State(tmp_path)
    state.write(
        {
            "setup_token": "offline",
            "napcat_token": "offline",
            "onebot_token": "offline",
        },
        "secrets.json",
    )
    state.write({"base_url": "https://example.invalid/v1"}, "ai.json")
    monkeypatch.setenv("QQBOT_NAPCAT_URL", "http://napcat:6099")
    fetch = Mock(side_effect=AssertionError("Do not inspect an existing deployment"))
    monkeypatch.setattr(smoke, "fetch_json", fetch)
    with pytest.raises(smoke.SmokeError, match="non-synthetic"):
        smoke.await_ready(state, 30, configured=True)
    fetch.assert_not_called()


@pytest.mark.parametrize("broken", ["vision", "processor", "plugin"])
def test_smoke_inspects_actual_configure_result_and_rejects_broken_bindings(
    tmp_path, monkeypatch, broken
):
    from bootstrap.configure import configure
    from test_public_config import FakeLangBot

    state = smoke.State(tmp_path)
    api = FakeLangBot()
    archive = "/opt/qq-ai-bot/.local/tools/7zip/linux-x64/7zz"
    monkeypatch.setenv("QQBOT_ARCHIVE_PATH", archive)
    record = configure(
        api,
        smoke.SYNTHETIC_AI,
        {},
        state.write,
        "offline-onebot-token",
        "http://app:5300",
        archive,
    )
    state.write(smoke.SYNTHETIC_AI, "ai.json")
    before = smoke.inspect_configuration(state, api)
    assert len(before["llms"]) == 2
    assert len(before["agents"]) == 2
    assert before["bots"] == [record["bot_uuid"]]
    configure(
        api,
        smoke.SYNTHETIC_AI,
        record,
        state.write,
        "offline-onebot-token",
        "http://app:5300",
        archive,
    )
    assert smoke.inspect_configuration(state, api) == before
    if broken == "vision":
        model = next(
            row
            for row in api.records["/api/v1/provider/models/llm"]
            if row["uuid"] == record["vision_model_uuid"]
        )
        model["provider_uuid"] = "wrong-provider"
    elif broken == "processor":
        bot = next(
            row
            for row in api.records["/api/v1/platform/bots"]
            if row["uuid"] == record["bot_uuid"]
        )
        bot["plugin_processors"][0]["enabled"] = False
    else:
        api.configs["langbot-team/AIImagePlugin"]["model_name"] = "wrong-model"
    with pytest.raises(smoke.SmokeError, match="Configuration contract"):
        smoke.inspect_configuration(state, api)
