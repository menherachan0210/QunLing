"""Run copied launchers with fake Docker/browser functions; no live services."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def find_bash():
    candidate = shutil.which("bash")
    if candidate and "WindowsApps" not in candidate:
        return candidate
    git = shutil.which("git")
    if git:
        candidate = Path(git).resolve().parent.parent / "bin/bash.exe"
        if candidate.is_file():
            return str(candidate)
    return None


BASH = find_bash()
# Windows .cmd entry points use Windows PowerShell; Linux CI uses PowerShell 7.
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

BASH_DRIVER = r"""
docker() {
  printf '%s\0' "$@" >> "$QQBOT_TEST_LOG"
  printf '\n' >> "$QQBOT_TEST_LOG"
  local operation="${4:-}"
  if [[ "${1:-}" == info ]]; then operation=info; fi
  if [[ "${2:-}" == version ]]; then operation=version; fi
  if [[ "$operation" == "${QQBOT_TEST_FAIL:-}" ]]; then return 23; fi
  case "$operation" in
    info) printf '%s\n' "$QQBOT_TEST_ENGINE" ;;
    run) printf '%s\n' 'http://127.0.0.1:5310/#token=offline-token' ;;
  esac
}
source "$QQBOT_TEST_SCRIPT" "$QQBOT_TEST_ACTION"
"""

POWERSHELL_DRIVER = r"""
$ErrorActionPreference = 'Stop'
function global:docker {
    $record = @{ arguments = @($args) }
    [IO.File]::AppendAllText($env:QQBOT_TEST_LOG, ($record | ConvertTo-Json -Compress) + "`n")
    $operation = if ($args.Count -ge 4) { $args[3] } else { '' }
    if ($args[0] -eq 'info') { $operation = 'info' }
    if ($args.Count -ge 2 -and $args[1] -eq 'version') { $operation = 'version' }
    $global:LASTEXITCODE = 0
    if ($operation -eq $env:QQBOT_TEST_FAIL) { $global:LASTEXITCODE = 23; return }
    if ($operation -eq 'info') { Write-Output $env:QQBOT_TEST_ENGINE }
    if ($operation -eq 'run') { Write-Output 'http://127.0.0.1:5310/#token=offline-token' }
}
function global:Start-Process {
    $record = @{ browser = @($args) }
    [IO.File]::AppendAllText($env:QQBOT_TEST_LOG, ($record | ConvertTo-Json -Compress) + "`n")
}
try {
    if ($env:QQBOT_TEST_WRAPPER -eq '1') {
        & $env:QQBOT_TEST_SCRIPT
    } else {
        & $env:QQBOT_TEST_SCRIPT -Action $env:QQBOT_TEST_ACTION -OpenBrowser
    }
} catch {
    [Console]::Error.WriteLine('Launcher returned an error.')
    exit 1
}
"""


@pytest.fixture
def launch(tmp_path):
    # This copy has no native .venv/NapCat. Compatibility wrappers must choose
    # public Compose mode, even though the developer checkout has a live bot.
    root = tmp_path / "project with spaces"
    root.mkdir()
    for name in (
        "bot.sh",
        "Public-Bot.ps1",
        "Start-Bot.ps1",
        "Open-Bot.ps1",
        "Stop-Bot.ps1",
    ):
        shutil.copyfile(ROOT / name, root / name)
    compose = root / "deploy/compose/compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services: {}\n", encoding="utf-8")
    driver = tmp_path / "driver.ps1"
    driver.write_text(POWERSHELL_DRIVER, encoding="utf-8")
    sequence = 0

    def run(kind, action="up", *, fail="", engine="linux/amd64", wrapper=None):
        nonlocal sequence
        sequence += 1
        log = tmp_path / f"calls-{sequence}.jsonl"
        script = root / (wrapper or ("bot.sh" if kind == "bash" else "Public-Bot.ps1"))
        environment = {
            **os.environ,
            "QQBOT_TEST_LOG": log.as_posix(),
            "QQBOT_TEST_SCRIPT": script.as_posix(),
            "QQBOT_TEST_ACTION": action,
            "QQBOT_TEST_FAIL": fail,
            "QQBOT_TEST_ENGINE": engine,
            "QQBOT_TEST_WRAPPER": "1" if wrapper else "0",
        }
        if kind == "bash":
            if not BASH:
                pytest.skip("Bash is unavailable")
            command = [BASH, "--noprofile", "--norc", "-c", BASH_DRIVER]
        else:
            if not POWERSHELL:
                pytest.skip("PowerShell is unavailable")
            command = [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(driver),
            ]
        result = subprocess.run(
            command,
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        calls, browsers = [], []
        if log.exists():
            if kind == "bash":
                calls = [
                    line.decode("utf-8").rstrip("\0").split("\0")
                    for line in log.read_bytes().splitlines()
                ]
            else:
                for line in log.read_text(encoding="utf-8").splitlines():
                    item = json.loads(line)
                    if "arguments" in item:
                        calls.append(item["arguments"])
                    else:
                        browsers.append(item["browser"])
        return result, calls, browsers, compose

    return run


def operations(calls):
    return [
        "info" if args[0] == "info" else "version" if args[1] == "version" else args[3]
        for args in calls
    ]


@pytest.mark.parametrize("kind", ["bash", "powershell"])
def test_up_builds_before_requesting_link_and_quotes_compose_path(launch, kind):
    result, calls, browsers, compose = launch(kind)
    assert result.returncode == 0, result.stderr
    assert operations(calls) == ["version", "info", "up", "run"]
    assert calls[2][4:] == ["-d", "--build"]
    assert calls[3][4:] == ["--rm", "--no-deps", "wizard", "access"]
    for args in calls[2:]:
        assert args[:2] == ["compose", "-f"]
        path = args[2]
        if os.name == "nt" and kind == "bash":
            # MSYS presents Temp as /tmp and converts it for native docker.exe.
            path = subprocess.run(
                [
                    BASH,
                    "--noprofile",
                    "--norc",
                    "-c",
                    'cygpath -w -- "$1"',
                    "test",
                    path,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        assert Path(path).resolve() == compose.resolve()
    assert "http://127.0.0.1:5310/#token=offline-token" in result.stdout
    if kind == "powershell":
        assert browsers == [["http://127.0.0.1:5310/#token=offline-token"]]


@pytest.mark.parametrize("kind", ["bash", "powershell"])
@pytest.mark.parametrize(
    "failed,expected",
    [
        ("version", ["version"]),
        ("info", ["version", "info"]),
        ("up", ["version", "info", "up"]),
        ("run", ["version", "info", "up", "run"]),
    ],
)
def test_up_stops_on_failure_without_opening_browser(launch, kind, failed, expected):
    result, calls, browsers, _ = launch(kind, fail=failed)
    assert result.returncode != 0
    assert operations(calls) == expected
    assert browsers == []


@pytest.mark.parametrize("kind", ["bash", "powershell"])
@pytest.mark.parametrize("engine", ["windows/amd64", "linux/arm64"])
def test_unsupported_engines_never_build(launch, kind, engine):
    result, calls, browsers, _ = launch(kind, engine=engine)
    assert result.returncode != 0
    assert operations(calls) == ["version", "info"]
    assert browsers == []


@pytest.mark.parametrize("kind", ["bash", "powershell"])
@pytest.mark.parametrize(
    "action,expected",
    [
        ("down", ["down"]),
        ("status", ["ps"]),
        ("logs", ["logs", "--tail=100", "app", "wizard", "napcat"]),
        ("url", ["run", "--rm", "--no-deps", "wizard", "access"]),
    ],
)
def test_management_commands_preserve_volumes_and_do_not_build(
    launch, kind, action, expected
):
    result, calls, _, _ = launch(kind, action)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 2
    assert calls[1][3:] == expected
    assert not {"--volumes", "-v", "--build"}.intersection(calls[1])
    failed, failed_calls, browsers, _ = launch(kind, action, fail=expected[0])
    assert failed.returncode != 0
    assert len(failed_calls) == 2
    assert browsers == []


@pytest.mark.parametrize(
    "wrapper,expected,fail",
    [
        ("Start-Bot.ps1", ["version", "info", "up", "run"], "up"),
        ("Open-Bot.ps1", ["version", "run"], "run"),
        ("Stop-Bot.ps1", ["version", "down"], "down"),
    ],
)
def test_clean_checkout_compatibility_wrappers_use_public_mode_and_propagate_errors(
    launch, wrapper, expected, fail
):
    result, calls, browsers, _ = launch("powershell", wrapper=wrapper)
    assert result.returncode == 0, result.stderr
    assert operations(calls) == expected
    if wrapper != "Stop-Bot.ps1":
        assert browsers == [["http://127.0.0.1:5310/#token=offline-token"]]
    result, calls, browsers, _ = launch("powershell", wrapper=wrapper, fail=fail)
    assert result.returncode != 0
    assert operations(calls)[-1] == fail
    assert browsers == []
