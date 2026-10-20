"""Shared-package build and worker lifecycle checks without a live QQ service."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


runtime = module("file_runtime_setup", "scripts/Setup-FileRuntime.py")
supervisor = module("file_runtime_supervisor", "scripts/Run-LangBot.py")
prepare = module("file_runtime_prepare", "deploy/compose/prepare.py")
patcher = module("file_runtime_patcher", "scripts/Apply-RuntimePatches.py")


def test_pins_and_isolated_python_import_path(tmp_path, monkeypatch):
    assert dict(runtime.pins()) == {
        "pypdf": "6.21.1", "python-docx": "1.2.0", "openpyxl": "3.1.5", "python-pptx": "1.0.2",
    }
    root = tmp_path / "public project"
    (root / "qunling").mkdir(parents=True)
    (root / "qunling/__init__.py").write_text("")
    purelib = tmp_path / "purelib"
    purelib.mkdir()
    monkeypatch.setattr(runtime.sysconfig, "get_path", lambda _: str(purelib))
    runtime.register_package(root)
    assert (purelib / "qunling-local.pth").read_text() == str(root.resolve()) + "\n"
    runtime.register_package(root)


def test_docker_and_native_launchers_share_data_and_worker():
    compose = (ROOT / "deploy/compose/compose.yaml").read_text()
    assert "QUNLING_DATA_DIR: /opt/qq-ai-bot/langbot/data/qunling" in compose
    for name in ("Start-LangBot.sh", "Start-LangBot.ps1"):
        source = (ROOT / "scripts" / name).read_text()
        assert "Setup-FileRuntime.py" in source and "Run-LangBot.py" in source
    allowlist = (ROOT / "deploy/compose/Dockerfile.dockerignore").read_text()
    assert "!qunling/**" in allowlist and "**/__pycache__/" in allowlist
    assert "!scripts/Run-LangBot.py" in allowlist
    updater = (ROOT / "deploy/updater/Dockerfile.dockerignore").read_text()
    # Updating deployment metadata does not need attachment code or parser deps.
    assert "!qunling" not in updater


def test_supervisor_stops_worker_when_host_exits(tmp_path, monkeypatch):
    children, start = [], supervisor._start
    def track(*args):
        child = start(*args)
        children.append(child)
        return child
    monkeypatch.setattr(supervisor, "_start", track)
    code = supervisor.supervise(
        [sys.executable, "-c", "import time; time.sleep(.3); raise SystemExit(7)"],
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path, env=os.environ.copy(), install_signals=False, grace=1,
    )
    assert code == 7 and len(children) == 2
    assert all(child.poll() is not None for child in children)


def test_supervisor_restarts_worker_and_cleans_children_on_stop(tmp_path, monkeypatch):
    marker = tmp_path / "starts"
    daemon = (
        "from pathlib import Path; import time; "
        f"p=Path({str(marker)!r}); count=int(p.read_text())+1 if p.exists() else 1; "
        "p.write_text(str(count)); "
        "time.sleep(0 if count==1 else 30)"
    )
    stop = threading.Event()
    timer = threading.Timer(1.5, stop.set)
    children, start = [], supervisor._start
    def track(*args):
        child = start(*args)
        children.append(child)
        return child
    monkeypatch.setattr(supervisor, "_start", track)
    timer.start()
    try:
        result = supervisor.supervise(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            [sys.executable, "-c", daemon], cwd=tmp_path, env=os.environ.copy(),
            stop=stop, install_signals=False, daemon_restart_delay=.1, grace=1,
        )
    finally:
        timer.cancel()
    assert result == 0 and int(marker.read_text()) >= 2
    assert len(children) >= 3 and all(child.poll() is not None for child in children)


def test_posix_cleanup_signals_group_after_host_leader_exits(monkeypatch):
    kill_group = Mock()
    monkeypatch.setattr(supervisor, "os", SimpleNamespace(name="posix", killpg=kill_group))
    child = Mock(pid=12345)
    child.poll.return_value = 0
    supervisor._stop(child, grace=1)
    kill_group.assert_called_once_with(12345, supervisor.signal.SIGTERM)


def test_real_native_supervisor_in_isolated_project(tmp_path):
    """Exercise Windows Job setup/POSIX process groups without importing LangBot."""
    root = tmp_path / "isolated project"
    (root / "scripts").mkdir(parents=True)
    (root / "langbot").mkdir()
    (root / "qunling").mkdir()
    shutil.copyfile(ROOT / "scripts/Run-LangBot.py", root / "scripts/Run-LangBot.py")
    (root / "qunling/__init__.py").write_text("")
    (root / "qunling/daemon.py").write_text(
        "import os,time\nfrom pathlib import Path\n"
        "Path('worker.pid').write_text(str(os.getpid()))\ntime.sleep(30)\n"
    )
    (root / "langbot/main.py").write_text(
        "import time\nfrom pathlib import Path\n"
        "for _ in range(100):\n"
        "    if Path('worker.pid').exists(): break\n"
        "    time.sleep(.02)\n"
        "else: raise SystemExit(9)\n"
    )
    env = os.environ.copy()
    env["QUNLING_DATA_DIR"] = str(root / "langbot/data/qunling")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    result = subprocess.run(
        [sys.executable, str(root / "scripts/Run-LangBot.py")], cwd=tmp_path,
        env=env, capture_output=True, text=True, timeout=10, **options,
    )
    assert result.returncode == 0, result.stderr
    assert (root / "langbot/worker.pid").is_file()


def test_sdk_provenance_patch_accepts_canonical_sdk_paths(tmp_path):
    path = "langbot_plugin/api/entities/builtin/runner/resources.py"
    current = ROOT / "langbot/.venv/Lib/site-packages" / path
    if not current.is_file():
        pytest.skip("Local SDK fixture is unavailable")
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    shutil.copyfile(current, target)
    source = (ROOT / "patches/sdk-tool-provenance.patch").read_text(encoding="utf-8")
    plan = patcher.build_plan("sdk-tool-provenance.patch", tmp_path, source, "sdk")
    patcher.execute_plans([plan], apply=True)
    assert "source_id: str | None" in target.read_text()
    assert patcher.build_plan("sdk-tool-provenance.patch", tmp_path, source, "sdk").state == "already-applied"


def test_plugin_patch_rejects_non_pinned_manifest_without_modifying_it(tmp_path):
    source = tmp_path / "plugin"
    source.mkdir()
    manifest = source / "manifest.yaml"
    body = "metadata:\n  name: LocalAgent\n  author: langbot-team\n  version: 0.0.0\n"
    manifest.write_text(body)
    with pytest.raises(ValueError, match="pinned upstream"):
        prepare.apply_local_agent_patch(source, ROOT)
    assert manifest.read_text() == body
