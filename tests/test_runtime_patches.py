"""Runtime patch installer tests; all writes stay in pytest temporary folders."""

import ast
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import venv

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "runtime_patches", ROOT / "scripts/Apply-RuntimePatches.py"
)
patcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = patcher
SPEC.loader.exec_module(patcher)


def diff(path, before="old", after="new", *, git_style=True):
    header = f"diff --git a/{path} b/{path}\n" if git_style else ""
    return header + f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{before}\n+{after}\n"


def write(root, path, value="old\n"):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="utf-8")
    return target


@pytest.mark.parametrize("git_style", [True, False])
def test_apply_is_explicit_and_second_run_is_idempotent(tmp_path, git_style):
    path = "src/langbot/example.py"
    target = write(tmp_path, path)
    source = diff(path, git_style=git_style)
    plan = patcher.build_plan("example.patch", tmp_path, source, "host")
    assert plan.state == "ready"
    patcher.execute_plans([plan], apply=False)
    assert target.read_text() == "old\n"
    patcher.execute_plans([plan], apply=True)
    assert target.read_text() == "new\n"
    second = patcher.build_plan("example.patch", tmp_path, source, "host")
    assert second.state == "already-applied"
    patcher.execute_plans([second], apply=True)
    assert target.read_text() == "new\n"


def test_sdk_mapping_in_linux_style_nested_git_directory(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    sdk_root = tmp_path / "langbot/.venv/lib/python3.12/site-packages"
    path = "langbot_plugin/api/definition/plugin.py"
    target = write(sdk_root, path)
    source = diff(patcher.SDK_PREFIX + path, git_style=False)
    plan = patcher.build_plan("sdk.patch", sdk_root, source, "sdk")
    assert plan.state == "ready"
    patcher.execute_plans([plan], apply=True)
    assert target.read_text() == "new\n"
    assert (
        patcher.build_plan("sdk.patch", sdk_root, source, "sdk").state
        == "already-applied"
    )


def test_mismatch_does_not_overwrite_local_changes(tmp_path):
    path = "src/langbot/example.py"
    target = write(tmp_path, path, "local edit\n")
    with pytest.raises(patcher.PatchError, match="does not match"):
        patcher.build_plan("example.patch", tmp_path, diff(path), "host")
    assert target.read_text() == "local edit\n"


def test_recheck_rejects_changes_since_preflight(tmp_path):
    path = "src/langbot/example.py"
    target = write(tmp_path, path)
    plan = patcher.build_plan("example.patch", tmp_path, diff(path), "host")
    target.write_text("changed concurrently\n")
    with pytest.raises(patcher.PatchError, match="changed after preflight"):
        patcher.execute_plans([plan], apply=True)
    assert target.read_text() == "changed concurrently\n"


@pytest.mark.parametrize("git_style", [True, False])
def test_test_files_are_excluded(tmp_path, git_style):
    path = "src/langbot/example.py"
    source = diff(path, git_style=git_style)
    test_path = "tests/unit_tests/test_new.py"
    if git_style:
        source += f"diff --git a/{test_path} b/{test_path}\nnew file mode 100644\n"
    source += f"--- /dev/null\n+++ b/{test_path}\n@@ -0,0 +1 @@\n+test\n"
    payload, paths = patcher.runtime_payload(source, "host")
    assert paths == (path,)
    assert b"tests/" not in payload


@pytest.mark.parametrize(
    "path",
    [
        "manifest.yaml",
        "../escape",
        "src/langbot/../../escape",
        "/etc/profile",
        "C:/example",
    ],
)
def test_rejects_non_runtime_or_unsafe_paths(path):
    with pytest.raises(patcher.PatchError):
        patcher.runtime_payload(diff(path), "host")


def test_patch_allowlist_excludes_plugin_source_patches():
    names = {name for name, _, _ in patcher.PATCHES}
    assert "qq-text-reader.patch" not in names
    assert "longterm-memory-compatibility.patch" not in names
    assert "ai-image-generation-editing.patch" not in names
    assert "onebot-group-context.patch" in names
    assert ("group-message-isolation.patch", "host", False) in patcher.PATCHES
    assert ("group-history-speakers.patch", "host", False) in patcher.PATCHES
    assert ("sdk-attachment-message-budget.patch", "sdk", False) in patcher.PATCHES
    assert ("host-managed-runtime-transport.patch", "host", False) in patcher.PATCHES
    assert ("host-persisted-plugin-installations.patch", "host", False) in patcher.PATCHES
    assert len(names) == 14


def test_all_bundled_patches_have_supported_runtime_blocks():
    for name, component, _ in patcher.PATCHES:
        payload, paths = patcher.runtime_payload(
            (ROOT / "patches" / name).read_text(encoding="utf-8"), component
        )
        assert payload
        assert paths
        assert all(not path.startswith("tests/") for path in paths)


def test_persisted_plugin_inventory_patch_roundtrips_upstream_route_context(tmp_path):
    path = "src/langbot/pkg/api/http/controller/groups/plugins.py"
    original = (
        "            return quart.Response('// SDK not found', status=404, mimetype='application/javascript')\n"
        "\n"
        "        @self.route(\n"
        "            '',\n"
        "            methods=['GET'],\n"
        "            auth_type=group.AuthType.USER_TOKEN_OR_API_KEY,\n"
    )
    target = write(tmp_path, path, original)
    name = "host-persisted-plugin-installations.patch"
    source = (ROOT / "patches" / name).read_text(encoding="utf-8")
    plan = patcher.build_plan(name, tmp_path, source, "host")
    assert plan.state == "ready"
    patcher.execute_plans([plan], apply=True)
    patched = target.read_text(encoding="utf-8")
    assert "'/_/installations'" in patched
    assert "request_context.workspace_uuid" in patched
    assert "plugin_connector" not in patched
    assert "config" not in patched and "install_info" not in patched
    assert "tests/" not in plan.payload.decode()
    assert patcher.build_plan(name, tmp_path, source, "host").state == "already-applied"
    assert patcher.git_apply(tmp_path, plan.payload, "--reverse").returncode == 0
    assert target.read_text(encoding="utf-8") == original


def test_sdk_version_and_location_are_validated(monkeypatch, tmp_path):
    info = {
        "platform": "linux",
        "purelib": str(tmp_path),
        "sdk_root": str(tmp_path / "langbot_plugin"),
        "sdk_version": "0.7.11",
    }
    monkeypatch.setattr(
        patcher.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout=json.dumps(info)),
    )
    assert patcher.inspect_python(Path("python"))["platform"] == "linux"
    info["sdk_version"] = "0.8.0"
    with pytest.raises(patcher.PatchError, match="0.7.11"):
        patcher.inspect_python(Path("python"))
    info["sdk_version"] = "0.7.11"
    info["sdk_root"] = str(tmp_path / "editable_checkout")
    with pytest.raises(patcher.PatchError, match="purelib"):
        patcher.inspect_python(Path("python"))


@pytest.mark.parametrize("explicit_python", [False, True])
def test_cli_uses_real_virtualenv_instead_of_symlink_target(
    monkeypatch, tmp_path, explicit_python
):
    """Exercise Python's real venv discovery without downloading the SDK."""
    project = tmp_path / "langbot"
    write(
        project,
        "pyproject.toml",
        '[project]\nname="langbot"\nversion="4.11.0"\n',
    )
    environment = (
        tmp_path / "selected environment" if explicit_python else project / ".venv"
    )
    venv.EnvBuilder(with_pip=False, symlinks=sys.platform != "win32").create(
        environment
    )
    python = environment / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    probe = "import sysconfig; print(sysconfig.get_path('purelib'))"
    purelib = Path(
        subprocess.check_output(
            [str(python), "-I", "-c", probe], text=True, encoding="utf-8"
        ).strip()
    ).resolve()
    assert purelib.is_relative_to(environment.resolve())
    if sys.platform != "win32":
        assert python.is_symlink()
        base_purelib = Path(
            subprocess.check_output(
                [str(python.resolve()), "-I", "-c", probe],
                text=True,
                encoding="utf-8",
            ).strip()
        ).resolve()
        assert base_purelib != purelib

    # A minimal distribution fixture is enough for the installer's real isolated
    # metadata probe; no SDK imports, network, or installed environment is changed.
    write(purelib, "langbot_plugin/__init__.py", "")
    target = write(purelib, "langbot_plugin/example.py")
    write(
        purelib,
        "langbot_plugin-0.7.11.dist-info/METADATA",
        "Metadata-Version: 2.1\nName: langbot-plugin\nVersion: 0.7.11\n",
    )
    write(
        tmp_path,
        "patches/example.patch",
        diff(patcher.SDK_PREFIX + "langbot_plugin/example.py"),
    )
    monkeypatch.setattr(patcher, "BASE_DIR", tmp_path)
    monkeypatch.setattr(patcher, "PATCHES", (("example.patch", "sdk", False),))
    original_build_plan = patcher.build_plan

    def only_temporary_environment(name, root, source, component):
        # If environment selection regresses, never patch a global installation.
        assert root == purelib
        return original_build_plan(name, root, source, component)

    monkeypatch.setattr(patcher, "build_plan", only_temporary_environment)
    monkeypatch.chdir(tmp_path)
    args = ["--langbot", "langbot", "--component", "sdk", "--apply"]
    if explicit_python:
        args += ["--python", str(python.relative_to(tmp_path))]
    assert patcher.main(args) == 0
    assert target.read_text(encoding="utf-8") == "new\n"


def test_linux_cli_skips_windows_patch_and_defaults_to_check_only(
    monkeypatch, tmp_path
):
    write(tmp_path, "pyproject.toml", '[project]\nname="langbot"\nversion="4.11.0"\n')
    info = {
        "platform": "linux",
        "purelib": str(tmp_path / "sdk"),
        "sdk_version": "0.7.11",
    }
    monkeypatch.setattr(patcher, "inspect_python", lambda _: info)
    observed = []
    monkeypatch.setattr(
        patcher,
        "build_plan",
        lambda name, root, source, component: observed.append((name, component))
        or name,
    )
    calls = []
    monkeypatch.setattr(
        patcher, "execute_plans", lambda plans, apply: calls.append((plans, apply))
    )
    assert patcher.main(["--langbot", str(tmp_path), "--python", sys.executable]) == 0
    assert len(observed) == 13
    assert ("host-persisted-plugin-installations.patch", "host") in observed
    assert ("group-message-isolation.patch", "host") in observed
    assert ("group-history-speakers.patch", "host") in observed
    assert ("sdk-attachment-message-budget.patch", "sdk") in observed
    assert all(name != "sdk-windows-dependency-temp.patch" for name, _ in observed)
    assert calls[0][1] is False


def test_invalid_host_version_stops_before_any_apply(monkeypatch, tmp_path):
    write(tmp_path, "pyproject.toml", '[project]\nname="langbot"\nversion="4.12.0"\n')
    monkeypatch.setattr(
        patcher, "execute_plans", lambda *a: pytest.fail("must not apply")
    )
    assert (
        patcher.main(["--langbot", str(tmp_path), "--component", "host", "--apply"])
        == 1
    )


def test_all_patches_are_preflighted_before_any_write(monkeypatch, tmp_path):
    write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname="langbot"\nversion="4.11.0"\n',
    )
    observed = []

    def build(name, root, source, component):
        observed.append(name)
        if len(observed) == 2:
            raise patcher.PatchError("second patch conflicts")
        return patcher.PatchPlan(name, root, b"unused", "ready")

    monkeypatch.setattr(patcher, "build_plan", build)
    monkeypatch.setattr(
        patcher, "execute_plans", lambda *a: pytest.fail("must not apply")
    )
    assert (
        patcher.main(["--langbot", str(tmp_path), "--component", "host", "--apply"])
        == 1
    )
    assert len(observed) == 2


def test_missing_runtime_target_is_rejected(tmp_path):
    with pytest.raises(patcher.PatchError, match="Missing or redirected"):
        patcher.build_plan(
            "example.patch", tmp_path, diff("src/langbot/missing.py"), "host"
        )


@pytest.mark.parametrize("text_patch_already_applied", [False, True])
def test_onebot_patches_preflight_together_before_any_write(
    tmp_path, text_patch_already_applied
):
    """Keep clean-install checks independent of earlier patches' added context."""
    fixture = ROOT / "tests" / "fixtures" / "onebot_runtime"
    prefix = "src/langbot/pkg/platform/adapters/aiocqhttp/"
    originals = {
        prefix + "platform_api.py": (fixture / "platform_api.py.txt").read_text(
            encoding="utf-8"
        ),
        prefix + "manifest.yaml": (fixture / "manifest.yaml").read_text(
            encoding="utf-8"
        ),
    }
    for path, source in originals.items():
        write(tmp_path, path, source)
    names = ("onebot-text-attachment.patch", "onebot-group-context.patch")
    sources = {
        name: (ROOT / "patches" / name).read_text(encoding="utf-8") for name in names
    }
    if text_patch_already_applied:
        text_plan = patcher.build_plan(names[0], tmp_path, sources[names[0]], "host")
        patcher.execute_plans([text_plan], apply=True)

    before_preflight = {
        path: (tmp_path / path).read_text(encoding="utf-8") for path in originals
    }
    # Match the installer: create every plan against the same untouched tree.
    plans = [
        patcher.build_plan(name, tmp_path, sources[name], "host") for name in names
    ]
    assert [plan.state for plan in plans] == [
        "already-applied" if text_patch_already_applied else "ready",
        "ready",
    ]
    patcher.execute_plans(plans, apply=False)
    assert {
        path: (tmp_path / path).read_text(encoding="utf-8") for path in originals
    } == before_preflight

    patcher.execute_plans(plans, apply=True)
    applied = {
        path: (tmp_path / path).read_text(encoding="utf-8") for path in originals
    }
    for action in (
        "get_group_file_url",
        "get_private_file_url",
        "get_group_recent_context",
    ):
        assert applied[prefix + "platform_api.py"].count(f"async def {action}(") == 1
        assert applied[prefix + "manifest.yaml"].count(f"- action: {action}") == 1
    second = [
        patcher.build_plan(name, tmp_path, sources[name], "host") for name in names
    ]
    assert all(plan.state == "already-applied" for plan in second)
    patcher.execute_plans(second, apply=True)
    assert {
        path: (tmp_path / path).read_text(encoding="utf-8") for path in originals
    } == applied

    for plan in reversed(second):
        assert (
            patcher.git_apply(tmp_path, plan.payload, "--reverse", "--check").returncode
            == 0
        )
        assert patcher.git_apply(tmp_path, plan.payload, "--reverse").returncode == 0
    assert {
        path: (tmp_path / path).read_text(encoding="utf-8") for path in originals
    } == originals


@pytest.mark.parametrize(
    "sdk_path",
    [".venv/Lib/site-packages", ".venv/lib/python3.12/site-packages"],
)
def test_attachment_budget_patch_is_narrow_and_idempotent(tmp_path, sdk_path):
    """The attachment budget must compose without changing other SDK behavior."""
    source = (ROOT / "patches/sdk-attachment-message-budget.patch").read_text(
        encoding="utf-8"
    )
    payload, paths = patcher.runtime_payload(source, "sdk")
    assert paths == ("langbot_plugin/runtime/io/connection.py",)
    removed = [
        line
        for line in payload.decode().splitlines()
        if line.startswith("-") and not line.startswith("---")
    ]
    added = [
        line
        for line in payload.decode().splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    assert removed == ["-MAX_MESSAGE_BYTES = 16 * 1024 * 1024"]
    assert added == ["+MAX_MESSAGE_BYTES = 32 * 1024 * 1024"]
    original = (
        "from __future__ import annotations\n\nimport abc\n\n\n"
        "MAX_MESSAGE_BYTES = 16 * 1024 * 1024\n"
        "# A byte limit alone is insufficient: an untrusted peer can fragment one\n"
        "# message into millions of empty or one-byte frames and exhaust Python object\n"
        "MAX_MESSAGE_FRAGMENTS = 4096\n"
    )
    sdk_root = tmp_path / sdk_path
    target = write(sdk_root, paths[0], original)
    plan = patcher.build_plan("attachment.patch", sdk_root, source, "sdk")
    assert plan.state == "ready"
    patcher.execute_plans([plan], apply=False)
    assert target.read_text(encoding="utf-8") == original
    patcher.execute_plans([plan], apply=True)
    expected = original.replace(
        "MAX_MESSAGE_BYTES = 16 * 1024 * 1024",
        "MAX_MESSAGE_BYTES = 32 * 1024 * 1024",
    )
    assert target.read_text(encoding="utf-8") == expected
    second = patcher.build_plan("attachment.patch", sdk_root, source, "sdk")
    assert second.state == "already-applied"
    patcher.execute_plans([second], apply=True)
    assert target.read_text(encoding="utf-8") == expected
    assert patcher.git_apply(sdk_root, payload, "--reverse").returncode == 0
    assert target.read_text(encoding="utf-8") == original


@pytest.fixture
def patched_transport(tmp_path):
    """Patch the unmodified upstream branch, then execute that resulting AST."""
    fixture = ROOT / "tests/fixtures/plugin_runtime/connector_transport.py.txt"
    original = fixture.read_text(encoding="utf-8")
    path = "src/langbot/pkg/plugin/connector.py"
    target = write(tmp_path, path, original)
    name = "host-managed-runtime-transport.patch"
    source = (ROOT / "patches" / name).read_text(encoding="utf-8")
    plan = patcher.build_plan(name, tmp_path, source, "host")
    assert plan.state == "ready"
    patcher.execute_plans([plan], apply=True)
    patched = target.read_text(encoding="utf-8")
    assert patcher.build_plan(name, tmp_path, source, "host").state == "already-applied"
    assert patcher.git_apply(tmp_path, plan.payload, "--reverse").returncode == 0
    assert target.read_text(encoding="utf-8") == original

    excerpt = patched[patched.index("            task_coro:") :]
    selection = next(
        node
        for node in ast.parse(textwrap.dedent(excerpt)).body
        if isinstance(node, ast.If)
    )
    function = ast.parse("async def select_transport():\n    pass").body[0]
    function.body = [
        selection,
        ast.Return(value=ast.Await(value=ast.Name(id="task_coro", ctx=ast.Load()))),
    ]
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    return compile(module, str(target), "exec")


@pytest.mark.parametrize(
    "host_platform,standalone,transport,expected",
    [
        ("docker", False, None, "external_ws"),
        ("docker", False, "stdio", "stdio"),
        ("docker", False, "unknown", "external_ws"),
        ("docker", True, None, "external_ws"),
        ("docker", True, "stdio", "external_ws"),
        ("linux", False, None, "stdio"),
        ("linux", False, "stdio", "stdio"),
        ("linux", True, None, "external_ws"),
        ("linux", True, "stdio", "external_ws"),
        ("win32", False, None, "managed_ws"),
        ("win32", False, "stdio", "managed_ws"),
        ("win32", True, None, "external_ws"),
        ("win32", True, "stdio", "external_ws"),
    ],
)
def test_host_runtime_transport_preserves_defaults_and_standalone_priority(
    monkeypatch, patched_transport, host_platform, standalone, transport, expected
):
    if transport is None:
        monkeypatch.delenv("LANGBOT_PLUGIN_RUNTIME_TRANSPORT", raising=False)
    else:
        monkeypatch.setenv("LANGBOT_PLUGIN_RUNTIME_TRANSPORT", transport)
    stdio = Mock(return_value=SimpleNamespace(run=AsyncMock(return_value="connected")))
    websocket = Mock(
        return_value=SimpleNamespace(run=AsyncMock(return_value="connected"))
    )
    host = SimpleNamespace(
        ap=SimpleNamespace(
            logger=Mock(),
            instance_config=SimpleNamespace(
                data={
                    "plugin": {"runtime_ws_url": "ws://configured.invalid/control/ws"}
                }
            ),
        ),
        _control_headers=Mock(return_value={"test-control": "synthetic"}),
        _control_token="synthetic",
        _start_runtime_subprocess=AsyncMock(),
        _connected=SimpleNamespace(set=Mock()),
    )
    platform = SimpleNamespace(
        get_platform=Mock(return_value=host_platform),
        use_websocket_to_connect_plugin_runtime=Mock(return_value=standalone),
    )
    callback = Mock()
    namespace = {
        "self": host,
        "platform": platform,
        "os": os,
        "sys": sys,
        "ws_client_controller": SimpleNamespace(WebSocketClientController=websocket),
        "stdio_client_controller": SimpleNamespace(StdioClientController=stdio),
        "PLUGIN_RUNTIME_CONTROL_TOKEN_ENV": "LANGBOT_PLUGIN_RUNTIME_CONTROL_TOKEN",
        "new_connection_callback": callback,
        "connect_errors": [],
    }
    exec(patched_transport, namespace)
    assert asyncio.run(namespace["select_transport"]()) == "connected"
    host.ctrl.run.assert_awaited_once_with(callback)
    if expected == "stdio":
        websocket.assert_not_called()
        host._start_runtime_subprocess.assert_not_called()
        host._control_headers.assert_not_called()
        assert stdio.call_args.kwargs["command"] == sys.executable
        assert stdio.call_args.kwargs["args"] == [
            "-m",
            "langbot_plugin.cli.__init__",
            "rt",
            "-s",
        ]
        assert stdio.call_args.kwargs["env"] == dict(os.environ)
    else:
        stdio.assert_not_called()
        managed = expected == "managed_ws"
        host._control_headers.assert_called_once_with(allow_generate=managed)
        assert websocket.call_args.kwargs["ws_url"] == (
            "ws://localhost:5400/control/ws"
            if managed
            else "ws://configured.invalid/control/ws"
        )
        if managed:
            host._start_runtime_subprocess.assert_awaited_once()
        else:
            host._start_runtime_subprocess.assert_not_called()
