"""Runtime patch installer tests; all writes stay in pytest temporary folders."""

import ast
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap
import time
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


@pytest.mark.parametrize('prerequisite_installed', [False, True])
def test_dependent_recall_overlay_preflights_without_writes_and_rechecks(tmp_path, prerequisite_installed):
    target = write(tmp_path, 'src/langbot/example.py', 'middle\n' if prerequisite_installed else 'old\n')
    entries = [('prerequisite.patch', diff('src/langbot/example.py', 'old', 'middle')),
               ('host-message-recall.patch', diff('src/langbot/example.py', 'middle', 'final'))]
    before = target.read_bytes()
    plans = patcher.build_host_plans(tmp_path, entries)
    assert target.read_bytes() == before
    assert plans[-1].state == 'ready'
    patcher.execute_plans(plans, apply=True)
    assert target.read_text() == 'final\n'
    repeated = patcher.build_host_plans(tmp_path, entries)
    assert all(plan.state == 'already-applied' for plan in repeated)
    patcher.execute_plans(repeated, apply=True)
    assert target.read_text() == 'final\n'


def test_independent_host_route_can_be_added_after_recall_overlay(tmp_path):
    write(tmp_path, 'src/langbot/example.py', 'final\n')
    write(tmp_path, 'src/langbot/routes.py', 'old\n')
    entries = [('prerequisite.patch', diff('src/langbot/example.py', 'old', 'middle')),
               ('new-route.patch', diff('src/langbot/routes.py', 'old', 'new')),
               ('host-message-recall.patch', diff('src/langbot/example.py', 'middle', 'final'))]
    plans = patcher.build_host_plans(tmp_path, entries)
    assert [p.state for p in plans] == ['already-applied', 'ready', 'already-applied']
    patcher.execute_plans(plans, True)
    assert all(p.state == 'already-applied' for p in patcher.build_host_plans(tmp_path, entries))


def test_invalid_dependent_overlay_never_partially_installs_prerequisite(tmp_path):
    target = write(tmp_path, 'src/langbot/example.py')
    entries = [('prerequisite.patch', diff('src/langbot/example.py', 'old', 'middle')),
               ('host-message-recall.patch', diff('src/langbot/example.py', 'wrong', 'final'))]
    with pytest.raises(patcher.PatchError):
        patcher.build_host_plans(tmp_path, entries)
    assert target.read_text() == 'old\n'


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


PROACTIVE_NAME = "host-proactive-chat.patch"
PROACTIVE_API = "src/langbot/pkg/platform/adapters/aiocqhttp/api_impl.py"
PROACTIVE_OLD_CALL = "            proactive_sent(self, native.group_id, message, raw, str(native.user_id))\n"
PROACTIVE_NEW_CALL = "            proactive_sent(self, native.group_id, message, raw, str(native.user_id), reply_to=str(native.message_id) if getattr(native, 'message_id', None) is not None else None)\n"


def proactive_originals(root, source):
    """Materialize all original hunk context; independent of ignored checkouts."""
    originals, path, cursor = {}, None, None
    for line in source.splitlines(keepends=True):
        if line.startswith("diff --git "):
            path = line.split(" b/", 1)[1].strip()
            originals[path] = []
            cursor = None
        elif line.startswith("@@ "):
            cursor = int(re.match(r"@@ -(\d+)", line)[1]) - 1
        elif cursor is not None and line[:1] in (" ", "-"):
            while len(originals[path]) <= cursor:
                originals[path].append("# unused upstream context\n")
            originals[path][cursor] = line[1:]
            cursor += 1
    for path, lines in originals.items():
        write(root, path, "".join(lines))
    return {path: (root / path).read_bytes() for path in originals}


@pytest.fixture
def legacy_proactive_install(tmp_path):
    source = (ROOT / "patches" / PROACTIVE_NAME).read_text(encoding="utf-8")
    original = proactive_originals(tmp_path, source)
    assert source.count("+" + PROACTIVE_NEW_CALL) == 1
    legacy = source.replace("+" + PROACTIVE_NEW_CALL, "+" + PROACTIVE_OLD_CALL)
    payload, _ = patcher.runtime_payload(legacy, "host")
    assert patcher.git_apply(tmp_path, payload, "--check").returncode == 0
    assert patcher.git_apply(tmp_path, payload).returncode == 0
    installed = {path: (tmp_path / path).read_bytes() for path in original}
    return source, original, installed


def test_proactive_new_install_applies_complete_patch_and_roundtrips(tmp_path):
    source = (ROOT / "patches" / PROACTIVE_NAME).read_text(encoding="utf-8")
    original = proactive_originals(tmp_path, source)
    plan = patcher.build_plan(PROACTIVE_NAME, tmp_path, source, "host")
    assert plan.state == "ready" and plan.upgrade_from is None
    patcher.execute_plans([plan], apply=False)
    assert {path: (tmp_path / path).read_bytes() for path in original} == original
    patcher.execute_plans([plan], apply=True)
    assert PROACTIVE_NEW_CALL in (tmp_path / PROACTIVE_API).read_text(encoding="utf-8")
    assert patcher.build_plan(PROACTIVE_NAME, tmp_path, source, "host").state == "already-applied"
    assert patcher.git_apply(tmp_path, plan.payload, "--reverse").returncode == 0
    assert {path: (tmp_path / path).read_bytes() for path in original} == original


def test_known_full_proactive_patch_upgrades_only_one_line_and_is_idempotent(tmp_path, legacy_proactive_install):
    source, original, installed = legacy_proactive_install
    plan = patcher.build_plan(PROACTIVE_NAME, tmp_path, source, "host")
    assert plan.state == "ready" and plan.upgrade_from and plan.verify_payload
    assert plan.payload.count(b"--- a/") == 1
    assert ("-" + PROACTIVE_OLD_CALL).encode() in plan.payload
    assert ("+" + PROACTIVE_NEW_CALL).encode() in plan.payload
    patcher.execute_plans([plan], apply=False)
    assert {path: (tmp_path / path).read_bytes() for path in original} == installed
    patcher.execute_plans([plan], apply=True)
    for path in original:
        expected = installed[path].replace(PROACTIVE_OLD_CALL.rstrip("\n").encode(), PROACTIVE_NEW_CALL.rstrip("\n").encode()) if path == PROACTIVE_API else installed[path]
        assert (tmp_path / path).read_bytes() == expected
    second = patcher.build_plan(PROACTIVE_NAME, tmp_path, source, "host")
    assert second.state == "already-applied" and second.upgrade_from is None
    patcher.execute_plans([second], apply=True)
    assert patcher.git_apply(tmp_path, second.payload, "--reverse").returncode == 0
    assert {path: (tmp_path / path).read_bytes() for path in original} == original


def test_proactive_upgrade_cli_default_reports_pending_without_writes(monkeypatch, tmp_path, legacy_proactive_install, capsys):
    _, _, installed = legacy_proactive_install
    write(tmp_path, "pyproject.toml", '[project]\nname="langbot"\nversion="4.11.0"\n')
    monkeypatch.setattr(patcher, "PATCHES", ((PROACTIVE_NAME, "host", False),))
    args = ["--langbot", str(tmp_path), "--component", "host"]
    assert patcher.main(args) == 0
    assert "ready: host-proactive-chat.patch" in capsys.readouterr().out
    assert {path: (tmp_path / path).read_bytes() for path in installed} == installed
    assert patcher.main(args + ["--apply"]) == 0
    assert PROACTIVE_NEW_CALL in (tmp_path / PROACTIVE_API).read_text(encoding="utf-8")


@pytest.mark.parametrize("after_preflight", [False, True])
def test_partial_or_changed_legacy_proactive_patch_is_never_upgraded(tmp_path, legacy_proactive_install, after_preflight):
    source, _, installed = legacy_proactive_install
    if after_preflight:
        plan = patcher.build_plan(PROACTIVE_NAME, tmp_path, source, "host")
    target = tmp_path / "src/langbot/pkg/pipeline/pool.py"
    target.write_text(target.read_text(encoding="utf-8").replace(
        "proactive_completed(query.adapter, query.message_event)", "locally_changed(query.adapter, query.message_event)", 1), encoding="utf-8")
    before = {path: (tmp_path / path).read_bytes() for path in installed}
    with pytest.raises(patcher.PatchError, match="changed after preflight" if after_preflight else "does not match"):
        if after_preflight:
            patcher.execute_plans([plan], apply=True)
        else:
            patcher.build_plan(PROACTIVE_NAME, tmp_path, source, "host")
    assert {path: (tmp_path / path).read_bytes() for path in installed} == before


def test_upgrade_exception_is_not_available_for_other_patch_names(tmp_path, legacy_proactive_install):
    source, _, installed = legacy_proactive_install
    with pytest.raises(patcher.PatchError, match="does not match"):
        patcher.build_plan("unrelated.patch", tmp_path, source, "host")
    assert {path: (tmp_path / path).read_bytes() for path in installed} == installed


def test_matching_but_unknown_complete_legacy_payload_is_rejected(tmp_path):
    source = (ROOT / "patches" / PROACTIVE_NAME).read_text(encoding="utf-8").replace(
        "await shutdown_proactive(self)", "await locally_changed(self)")
    original = proactive_originals(tmp_path, source)
    legacy, _ = patcher.runtime_payload(source.replace("+" + PROACTIVE_NEW_CALL, "+" + PROACTIVE_OLD_CALL), "host")
    assert patcher.git_apply(tmp_path, legacy).returncode == 0
    assert patcher.git_apply(tmp_path, legacy, "--reverse", "--check").returncode == 0
    installed = {path: (tmp_path / path).read_bytes() for path in original}
    with pytest.raises(patcher.PatchError, match="does not match"):
        patcher.build_plan(PROACTIVE_NAME, tmp_path, source, "host")
    assert {path: (tmp_path / path).read_bytes() for path in original} == installed


@pytest.mark.parametrize("native_fields,expected", [({"message_id": 123}, "123"), ({"message_id": None}, None), ({}, None)])
def test_patched_reply_call_passes_actual_source_id_without_stringifying_none(native_fields, expected):
    sent = Mock()
    native = SimpleNamespace(group_id=100, user_id=200, **native_fields)
    namespace = {"proactive_sent": sent, "native": native, "self": object(), "message": object(), "raw": {"message_id": 456}}
    source = (ROOT / "patches" / PROACTIVE_NAME).read_text(encoding="utf-8")
    call = next(line[1:].strip() for line in source.splitlines() if line.startswith("+" + PROACTIVE_NEW_CALL.strip("\n")))
    exec(call, namespace)
    assert sent.call_args.kwargs == {"reply_to": expected}
    assert sent.call_args.args[-1] == "200"


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
    assert ("sdk-tool-provenance.patch", "sdk", False) in patcher.PATCHES
    assert ("file-outbox-host.patch", "host", False) in patcher.PATCHES
    assert ("sdk-qunling-worker-storage.patch", "sdk", False) in patcher.PATCHES
    assert ("host-tool-password-redaction.patch", "host", False) in patcher.PATCHES
    assert ("host-proactive-chat.patch", "host", False) in patcher.PATCHES
    assert ("host-proactive-context.patch", "host", False) in patcher.PATCHES
    assert ("host-qq-status.patch", "host", False) in patcher.PATCHES
    assert ("host-qq-member-sessions.patch", "host", False) in patcher.PATCHES
    assert ("host-qq-dispatch.patch", "host", False) in patcher.PATCHES
    assert ("onebot-group-information.patch", "host", False) in patcher.PATCHES
    assert ("onebot-forward-placeholder.patch", "host", False) in patcher.PATCHES
    assert ("host-plugin-health.patch", "host", False) in patcher.PATCHES
    assert ("sdk-transport-recovery.patch", "sdk", False) in patcher.PATCHES
    assert ("host-request-diagnostics.patch", "host", False) in patcher.PATCHES
    assert ("sdk-utf8-chunk-performance.patch", "sdk", False) in patcher.PATCHES
    assert ("host-group-memory-reset.patch", "host", False) in patcher.PATCHES
    assert ("host-admin-console.patch", "host", False) in patcher.PATCHES
    assert ("host-qq-actions.patch", "host", False) in patcher.PATCHES
    assert ("host-reply-preferences.patch", "host", False) in patcher.PATCHES
    assert ("host-chinese-embedding.patch", "host", False) in patcher.PATCHES
    assert ("host-dialogue-provenance.patch", "host", False) in patcher.PATCHES
    assert ("host-dialogue-review.patch", "host", False) in patcher.PATCHES
    assert ("host-message-recall.patch", "host", False) in patcher.PATCHES
    assert ("host-console-preview.patch", "host", False) in patcher.PATCHES
    assert len(names) == 43
    assert "host-model-usage.patch" in names
    assert {"host-web-knowledge.patch", "host-memory-recall.patch"} <= names
    assert "host-animation-vision.patch" in names


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
            [str(python), "-I", "-X", "utf8", "-c", probe], text=True, encoding="utf-8"
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
    monkeypatch.setattr(patcher, 'build_host_plans', lambda root, entries:
                        [patcher.build_plan(name, root, source, 'host') for name, source in entries])
    calls = []
    monkeypatch.setattr(
        patcher, "execute_plans", lambda plans, apply: calls.append((plans, apply))
    )
    assert patcher.main(["--langbot", str(tmp_path), "--python", sys.executable]) == 0
    assert len(observed) == len(patcher.PATCHES) - 1
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
    monkeypatch.setattr(patcher, 'build_host_plans', lambda root, entries:
                        [patcher.build_plan(name, root, source, 'host') for name, source in entries])
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


@pytest.fixture
def group_history_bridge(tmp_path):
    """Execute the published Host patch without importing LangBot or its SDK."""
    fixture = ROOT / "tests/fixtures/onebot_runtime"
    prefix = "src/langbot/pkg/platform/adapters/aiocqhttp/"
    target = write(
        tmp_path,
        prefix + "platform_api.py",
        (fixture / "platform_api.py.txt").read_text(encoding="utf-8"),
    )
    write(
        tmp_path,
        prefix + "manifest.yaml",
        (fixture / "manifest.yaml").read_text(encoding="utf-8"),
    )
    for name in ("onebot-text-attachment.patch", "onebot-group-context.patch"):
        source = (ROOT / "patches" / name).read_text(encoding="utf-8")
        plan = patcher.build_plan(name, tmp_path, source, "host")
        patcher.execute_plans([plan], apply=True)
    tree = ast.parse(target.read_text(encoding="utf-8"))
    tree.body = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.Import)
            and any(alias.name == "aiocqhttp" for alias in node.names)
        )
    ]
    namespace = {}
    exec(compile(tree, str(target), "exec"), namespace)
    # Isolate the clock replacement from asyncio and other tests.
    namespace["time"] = SimpleNamespace(
        time=lambda: 1800000000, monotonic=time.monotonic
    )
    return SimpleNamespace(**namespace)


def group_history_message(message_id, timestamp, text="Synthetic discussion", **changes):
    return {
        "message_id": str(message_id),
        "time": timestamp,
        "message_type": "group",
        "group_id": "20001",
        "sender": {"user_id": "30001", "nickname": "Synthetic member"},
        "message": [{"type": "text", "data": {"text": text}}],
        **changes,
    }


def group_history_params(**changes):
    return {
        "group_id": "20001",
        "before_message_id": "-500",
        "before_time": 1800000000,
        **changes,
    }


def synthetic_history_bot(rows, *, descending=False):
    bot = AsyncMock()
    positions = {row["message_id"]: index for index, row in enumerate(rows)}

    async def call(action, **parameters):
        assert action == "get_group_msg_history"
        assert parameters["group_id"] == "20001"
        assert parameters["count"] == 100
        assert parameters["reverse_order"] is True
        position = positions[parameters["message_seq"]]
        page = rows[max(0, position - 99):position + 1]
        return {"messages": list(reversed(page)) if descending else list(page)}

    bot.call_action.side_effect = call
    return bot


def test_published_group_history_defaults_page_to_24h_1000_messages(group_history_bridge):
    now = 1800000000
    rows = [
        group_history_message(i + 1, now - 8000 + i, "x" * 1000)
        for i in range(1200)
    ]
    bot = synthetic_history_bot([*rows, group_history_message(-500, now)])
    result = asyncio.run(
        group_history_bridge.get_group_recent_context(bot, group_history_params())
    )
    assert result["window_seconds"] == 86400
    assert result["max_messages"] == 1000
    assert [row["message_id"] for row in result["messages"]] == [
        str(i) for i in range(201, 1201)
    ]
    assert sum(len(row["text"]) for row in result["messages"]) == 1000000
    assert all(row["time"] < now - 3600 for row in result["messages"])
    assert result["truncated"] is True
    assert result["stop_reason"] == "limit"
    assert bot.call_action.await_count == 11


@pytest.mark.parametrize("descending", [False, True])
def test_published_group_history_same_second_page_boundaries(group_history_bridge, descending):
    now = 1800000000
    rows = [group_history_message(10000 - 7 * i, now - 1) for i in range(250)]
    bot = synthetic_history_bot(
        [*rows, group_history_message(-500, now)], descending=descending
    )
    result = asyncio.run(
        group_history_bridge.get_group_recent_context(bot, group_history_params())
    )
    assert [row["message_id"] for row in result["messages"]] == [
        row["message_id"] for row in rows
    ]
    assert result["truncated"] is False
    assert bot.call_action.await_count == 3


@pytest.mark.parametrize("descending", [False, True])
@pytest.mark.parametrize("all_same_second", [False, True])
def test_published_group_history_excludes_current_and_later_messages(
    group_history_bridge, descending, all_same_second
):
    now = 1800000000
    rows = [
        group_history_message(1, now if all_same_second else now - 1),
        group_history_message(2, now),
        group_history_message(-500, now),
        group_history_message(3, now),
    ]
    bot = AsyncMock()
    bot.call_action.return_value = {
        "messages": list(reversed(rows)) if descending else rows
    }
    result = asyncio.run(
        group_history_bridge.get_group_recent_context(bot, group_history_params())
    )
    assert [row["message_id"] for row in result["messages"]] == (
        [] if all_same_second else ["1", "2"]
    )
    if all_same_second:
        assert result["truncated"] is True
        assert result["stop_reason"] == "ambiguous_order"


def test_published_group_history_repeat_cursor_stops_with_partial_result(group_history_bridge, monkeypatch):
    # Exercise cursor repetition independently of CPU pressure during the full
    # image suite. This namespace is isolated from asyncio's actual clock.
    monkeypatch.setattr(group_history_bridge.time, 'monotonic', lambda: 0.0)
    bot = AsyncMock()
    bot.call_action.return_value = {
        "messages": [
            group_history_message(i + 1, 1800000000 - 100 + i)
            for i in range(100)
        ]
    }
    result = asyncio.run(
        group_history_bridge.get_group_recent_context(bot, group_history_params())
    )
    assert len(result["messages"]) == 100
    assert len({row["message_id"] for row in result["messages"]}) == 100
    assert result["truncated"] is True
    assert result["stop_reason"] == "no_progress"
    assert bot.call_action.await_count == 2


@pytest.mark.parametrize("descending", [False, True])
def test_published_group_history_first_full_same_second_page_is_fail_closed(
    group_history_bridge, descending
):
    now = 1800000000
    rows = [
        *[group_history_message(i + 1, now) for i in range(98)],
        group_history_message(-500, now),
        group_history_message(9999, now),
    ]
    bot = AsyncMock()
    bot.call_action.return_value = {
        "messages": list(reversed(rows)) if descending else rows
    }
    result = asyncio.run(
        group_history_bridge.get_group_recent_context(bot, group_history_params())
    )
    assert result["messages"] == []
    assert result["truncated"] is True
    assert result["stop_reason"] == "ambiguous_order"
    assert bot.call_action.await_count == 1


@pytest.mark.parametrize(
    "failure,reason",
    [
        (TimeoutError("private upstream detail"), "timeout"),
        (RuntimeError("private upstream detail"), "platform_error"),
        ({"messages": "private invalid data"}, "invalid_page"),
    ],
)
def test_published_group_history_later_failure_preserves_partial_result(
    group_history_bridge, failure, reason
):
    bot = AsyncMock()
    bot.call_action.side_effect = [
        {"messages": [
            group_history_message(i + 1, 1800000000 - 100 + i)
            for i in range(100)
        ]},
        failure,
    ]
    result = asyncio.run(
        group_history_bridge.get_group_recent_context(bot, group_history_params())
    )
    assert len(result["messages"]) == 100
    assert result["truncated"] is True
    assert result["stop_reason"] == reason
    assert "private" not in repr(result)
    assert bot.call_action.await_count == 2


def test_published_group_history_marks_incomplete_message_evidence(group_history_bridge):
    bot = AsyncMock()
    bot.call_action.return_value = {"messages": [
        group_history_message(1, 1800000000 - 2, "x" * 1001),
        group_history_message(2, 1800000000 - 1, "Complete statement"),
    ]}
    result = asyncio.run(
        group_history_bridge.get_group_recent_context(bot, group_history_params())
    )
    assert len(result["messages"][0]["text"]) == 1000
    assert result["messages"][0]["truncated"] is True
    assert "truncated" not in result["messages"][1]


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
