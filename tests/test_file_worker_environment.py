"""Exercise the SDK's actual worker environment, not the pytest parent's env."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from qunling.jobs import JobService
from qunling.scope import Scope


ROOT = Path(__file__).resolve().parents[1]
PATCH_NAME = "sdk-qunling-worker-storage.patch"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def patched_sdk(tmp_path):
    sdk = pytest.importorskip("langbot_plugin")
    patcher = load_module("file_environment_patch_installer", ROOT / "scripts/Apply-RuntimePatches.py")
    sdk_root = Path(sdk.__file__).parent.parent
    paths = ["langbot_plugin/runtime/plugin/worker_launcher.py", "langbot_plugin/runtime/plugin/mgr.py"]
    for path in paths:
        target = tmp_path / "sdk" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sdk_root / path, target)
    source = (ROOT / "patches" / PATCH_NAME).read_text(encoding="utf-8")
    plan = patcher.build_plan(PATCH_NAME, tmp_path / "sdk", source, "sdk")
    patcher.execute_plans([plan], apply=True)
    assert patcher.build_plan(PATCH_NAME, tmp_path / "sdk", source, "sdk").state == "already-applied"
    launcher = load_module("file_environment_worker_launcher", tmp_path / "sdk" / paths[0])
    manager = load_module("file_environment_worker_manager", tmp_path / "sdk" / paths[1])
    return SimpleNamespace(launcher=launcher, manager=manager, sdk_root=sdk_root)


def launch(patched_sdk, tmp_path, platform):
    # A tiny installed dependency tree exposes only the public shared package;
    # no request environment or data path is added by the test itself.
    dependencies = tmp_path / "dependencies"
    shutil.copytree(ROOT / "qunling", dependencies / "qunling", ignore=shutil.ignore_patterns("__pycache__"))
    code = tmp_path / "plugin"
    code.mkdir()
    spec = SimpleNamespace(
        artifact=SimpleNamespace(digest="synthetic", code_path=code, plugin_author="local", plugin_name="QQTextReader"),
        paths=SimpleNamespace(root_path=tmp_path / "installation"), registration_capability="synthetic-capability",
        dependency_environment=SimpleNamespace(artifact_digest="synthetic", site_packages_path=dependencies),
        runtime_ws_url="ws://127.0.0.1:1/unused",
    )
    launcher = patched_sdk.launcher.PluginWorkerLauncher(platform=platform, runtime_import_root=str(patched_sdk.sdk_root))
    launcher.configure(SimpleNamespace(require_hard_limits=False), "oss_dev")
    return launcher, spec


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_direct_worker_forwards_only_explicit_storage_not_parent_secrets(patched_sdk, tmp_path, monkeypatch, platform):
    directory = str(tmp_path / "shared-data")
    monkeypatch.setenv("QUNLING_DATA_DIR", directory)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-do-not-inherit")
    monkeypatch.setenv("QUNLING_UNRELATED_SECRET", "synthetic-do-not-inherit")
    monkeypatch.setenv("PYTHONPATH", "untrusted-parent-path")
    launcher, spec = launch(patched_sdk, tmp_path, platform)
    controller = launcher.create_controller(spec)
    assert controller.env["QUNLING_DATA_DIR"] == directory
    assert controller.env["PATH"] == os.environ["PATH"]
    assert "OPENAI_API_KEY" not in controller.env
    assert "QUNLING_UNRELATED_SECRET" not in controller.env
    assert "untrusted-parent-path" not in controller.env["PYTHONPATH"]


def test_unconfigured_and_relative_storage_do_not_create_an_implicit_working_directory(patched_sdk, monkeypatch):
    helper = patched_sdk.launcher.PluginWorkerLauncher._qunling_worker_environment
    monkeypatch.delenv("QUNLING_DATA_DIR", raising=False)
    assert helper() == {}
    monkeypatch.setenv("QUNLING_DATA_DIR", "relative-data")
    with pytest.raises(RuntimeError, match="absolute host path"):
        helper()


def test_shared_sandbox_does_not_inherit_host_storage(patched_sdk, tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path / "private-data"))
    launcher, spec = launch(patched_sdk, tmp_path, "linux")
    launcher.effective_profile = "shared"
    launcher.nsjail_path = "/unused/nsjail"
    monkeypatch.setattr(launcher, "build_nsjail_args", lambda _: ["synthetic"])
    controller = launcher.create_controller(spec)
    assert controller.env == {}


def test_legacy_direct_worker_uses_the_same_storage_contract(patched_sdk, tmp_path, monkeypatch):
    directory = str(tmp_path / "shared-data")
    monkeypatch.setenv("QUNLING_DATA_DIR", directory)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-do-not-inherit")
    launcher, _ = launch(patched_sdk, tmp_path, "linux")
    manager = patched_sdk.manager
    monkeypatch.setattr(manager, "get_platform", lambda: "linux")
    monkeypatch.setattr(manager.pkgmgr_helper, "get_plugin_site_packages", lambda _: None)
    instance = SimpleNamespace(worker_launcher=launcher)
    result = manager.PluginManager._legacy_plugin_environment(instance, "unused", "synthetic-capability")
    assert result["QUNLING_DATA_DIR"] == directory and "OPENAI_API_KEY" not in result
    assert result["PATH"] == os.environ["PATH"]


def test_linux_child_can_find_the_host_configured_decoder(patched_sdk, tmp_path, monkeypatch):
    directory = tmp_path / "shared-data"
    monkeypatch.setenv("QUNLING_DATA_DIR", str(directory))
    tools = tmp_path / "host-tools"
    tools.mkdir()
    executable = tools / ("qunling-test-decoder.exe" if os.name == "nt" else "qunling-test-decoder")
    executable.write_bytes(b"synthetic non-executed lookup fixture")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))
    launcher, spec = launch(patched_sdk, tmp_path, "linux")
    controller = launcher.create_controller(spec)
    code = "import shutil; print(shutil.which('" + executable.name + "') or 'not-found')"
    result = subprocess.run(
        [controller.command, "-c", code], cwd=controller.working_dir, env=controller.env,
        capture_output=True, text=True, timeout=10,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == executable


def test_real_isolated_worker_creates_xlsx_in_the_shared_durable_store(patched_sdk, tmp_path, monkeypatch):
    pytest.importorskip("openpyxl")
    directory = tmp_path / "shared-data"
    monkeypatch.setenv("QUNLING_DATA_DIR", str(directory))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-do-not-inherit")
    launcher, spec = launch(patched_sdk, tmp_path, "win32" if os.name == "nt" else "linux")
    controller = launcher.create_controller(spec)
    code = r'''
import asyncio, json, os
from qunling.assets import data_root
from qunling.jobs import JobService
from qunling.scope import Scope
async def main():
    assert "OPENAI_API_KEY" not in os.environ
    service = JobService(data_root())
    scope = Scope("workspace", "bot", "group:123456", "member")
    submitted = service.submit(scope, "create", {
        "format": "xlsx", "filename": "ranking.xlsx",
        "payload": {"sheets": [{"name": "Ranking", "rows": [["Name", "Rank"], ["Example", 1]]}]},
    }, "synthetic-request")
    worker = service.worker()
    await worker.run_once()
    await worker.wait_idle()
    print(json.dumps(service.snapshot(scope, submitted["task_id"])))
asyncio.run(main())
'''
    result = subprocess.run(
        [controller.command, "-c", code], cwd=controller.working_dir, env=controller.env,
        capture_output=True, text=True, encoding="utf-8", timeout=30,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["status"] == "completed", output
    host = JobService(directory)
    scope = Scope("workspace", "bot", "group:123456", "member")
    assert host.snapshot(scope, output["task_id"])["result_refs"] == output["result_refs"]
    assert host.assets.read(scope, output["result_refs"][0]).startswith(b"PK\x03\x04")
