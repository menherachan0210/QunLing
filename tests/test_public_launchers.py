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
  if [[ "${1:-}" == volume ]]; then operation=inspect; fi
  if [[ "${2:-}" == version ]]; then operation=version; fi
  if [[ "$operation" == run && "${@: -2:1}" == updater ]]; then operation="${@: -1}"; fi
  if [[ "$operation" == "${QQBOT_TEST_FAIL:-}" ]]; then return 23; fi
  case "$operation" in
    info) printf '%s\n' "$QQBOT_TEST_ENGINE" ;;
    inspect) [[ "$QQBOT_TEST_UPDATE_VOLUME" == 1 ]] ;;
    resume) return "$QQBOT_TEST_RESUME_CODE" ;;
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
    if ($args[0] -eq 'volume') { $operation = 'inspect' }
    if ($args.Count -ge 2 -and $args[1] -eq 'version') { $operation = 'version' }
    if ($operation -eq 'run' -and $args[$args.Count - 2] -eq 'updater') { $operation = $args[$args.Count - 1] }
    $global:LASTEXITCODE = 0
    if ($operation -eq $env:QQBOT_TEST_FAIL) { $global:LASTEXITCODE = 23; return }
    if ($operation -eq 'info') { Write-Output $env:QQBOT_TEST_ENGINE }
    if ($operation -eq 'inspect' -and $env:QQBOT_TEST_UPDATE_VOLUME -ne '1') { $global:LASTEXITCODE = 1; return }
    if ($operation -eq 'resume') { $global:LASTEXITCODE = [int]$env:QQBOT_TEST_RESUME_CODE; return }
    if ($operation -eq 'run') { Write-Output 'http://127.0.0.1:5310/#token=offline-token' }
}
function global:Start-Process {
    $record = @{ browser = @($args) }
    [IO.File]::AppendAllText($env:QQBOT_TEST_LOG, ($record | ConvertTo-Json -Compress) + "`n")
}
function global:Invoke-WebRequest {
    # Native compatibility tests must not probe the real local LangBot.
    return @{ StatusCode = 200 }
}
try {
    if ($env:QQBOT_TEST_WRAPPER -eq '1') {
        & $env:QQBOT_TEST_SCRIPT
    } else {
        & $env:QQBOT_TEST_SCRIPT -Action $env:QQBOT_TEST_ACTION -OpenBrowser
    }
} catch {
    [Console]::Error.WriteLine('Launcher returned an error: ' + $_.Exception.Message)
    exit 1
}
"""


@pytest.fixture
def launch(tmp_path):
    # This copy has no native .venv/NapCat. Compatibility wrappers must choose
    # public Compose mode, even though the developer checkout has a live bot.
    root = tmp_path / "project with spaces"
    root.mkdir()
    (root / "scripts").mkdir()
    for name in (
        "bot.sh",
        "Public-Bot.ps1",
        "Start-Bot.ps1",
        "Open-Bot.ps1",
        "Stop-Bot.ps1",
    ):
        relative = Path(name) if name == "bot.sh" else Path("scripts") / name
        shutil.copyfile(ROOT / relative, root / relative)
    compose = root / "deploy/compose/compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services: {}\n", encoding="utf-8")
    driver = tmp_path / "driver.ps1"
    driver.write_text(POWERSHELL_DRIVER, encoding="utf-8")
    sequence = 0

    def run(
        kind,
        action="up",
        *,
        fail="",
        engine="linux/amd64",
        wrapper=None,
        native=False,
        update_volume=False,
        resume_code=0,
        project="qq-ai-bot",
    ):
        nonlocal sequence
        sequence += 1
        log = tmp_path / f"calls-{sequence}.jsonl"
        name = wrapper or ("bot.sh" if kind == "bash" else "Public-Bot.ps1")
        script = root / name if name == "bot.sh" else root / "scripts" / name
        if native:
            python = root / "langbot/.venv/Scripts/python.exe"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
            (root / "napcat").mkdir(exist_ok=True)
            for component, actions in (
                ("NapCat", ("Start", "Open", "Stop")),
                ("LangBot", ("Start", "Stop")),
            ):
                directory = root / ("napcat" if component == "NapCat" else "scripts")
                for operation in actions:
                    (directory / f"{operation}-{component}.ps1").write_text(
                        f"Write-Output 'synthetic-native-{operation}-{component}'\n",
                        encoding="utf-8",
                    )
        environment = {
            **os.environ,
            "QQBOT_TEST_LOG": log.as_posix(),
            "QQBOT_TEST_SCRIPT": script.as_posix(),
            "QQBOT_TEST_ACTION": action,
            "QQBOT_TEST_FAIL": fail,
            "QQBOT_TEST_ENGINE": engine,
            "QQBOT_TEST_WRAPPER": "1" if wrapper else "0",
            "QQBOT_TEST_UPDATE_VOLUME": "1" if update_volume else "0",
            "QQBOT_TEST_RESUME_CODE": str(resume_code),
            "COMPOSE_PROJECT_NAME": project,
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
    result = []
    for args in calls:
        if args[0] == "volume":
            continue
        operation = (
            "info"
            if args[0] == "info"
            else "version"
            if args[1] == "version"
            else args[3]
        )
        if operation == "run" and args[-2] == "updater":
            operation = args[-1]
        result.append(operation)
    return result


@pytest.mark.parametrize("kind", ["bash", "powershell"])
def test_up_builds_before_requesting_link_and_quotes_compose_path(launch, kind):
    result, calls, browsers, compose = launch(kind)
    assert result.returncode == 0, result.stderr
    assert operations(calls) == ["version", "info", "up", "adopt", "run"]
    commands = [args for args in calls if args[:2] == ["compose", "-f"]]
    assert commands[0][3:] == ["up", "-d", "--build"]
    assert commands[1][3:] == ["run", "--rm", "--no-deps", "updater", "adopt"]
    assert commands[2][3:] == ["run", "--rm", "--no-deps", "wizard", "access"]
    for args in commands:
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
        ("adopt", ["version", "info", "up", "adopt"]),
        ("run", ["version", "info", "up", "adopt", "run"]),
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
def test_up_resumes_saved_snapshot_without_building_local_checkout(launch, kind):
    result, calls, _, _ = launch(kind, update_volume=True, project="isolated-bot")
    assert result.returncode == 0, result.stderr
    assert operations(calls) == ["version", "info", "guard", "resume", "run"]
    inspections = [args for args in calls if args[0] == "volume"]
    assert inspections
    assert all(
        args == ["volume", "inspect", "isolated-bot_update-data"]
        for args in inspections
    )
    assert all("--build" not in args for args in calls)
    assert next(args for args in calls if args[-1] == "resume")[3:] == [
        "run",
        "--rm",
        "--no-deps",
        "updater",
        "resume",
    ]


@pytest.mark.parametrize("kind", ["bash", "powershell"])
def test_missing_snapshot_code_three_falls_back_to_first_build(launch, kind):
    result, calls, _, _ = launch(kind, update_volume=True, resume_code=3)
    assert result.returncode == 0, result.stderr
    assert operations(calls) == [
        "version",
        "info",
        "guard",
        "resume",
        "up",
        "adopt",
        "run",
    ]


@pytest.mark.parametrize("kind", ["bash", "powershell"])
@pytest.mark.parametrize("code", [1, 23])
def test_resume_failure_does_not_rebuild_or_open_browser(launch, kind, code):
    result, calls, browsers, _ = launch(kind, update_volume=True, resume_code=code)
    assert result.returncode != 0
    assert operations(calls) == ["version", "info", "guard", "resume"]
    assert browsers == []


@pytest.mark.parametrize("kind", ["bash", "powershell"])
@pytest.mark.parametrize("action", ["up", "down", "rebuild"])
def test_active_update_guard_prevents_lifecycle_changes(launch, kind, action):
    result, calls, browsers, _ = launch(kind, action, update_volume=True, fail="guard")
    assert result.returncode != 0
    expected = ["version"] + (["info"] if action != "down" else []) + ["guard"]
    assert operations(calls) == expected
    assert browsers == []


@pytest.mark.parametrize("kind", ["bash", "powershell"])
def test_idle_down_checks_guard_and_keeps_all_volumes(launch, kind):
    result, calls, browsers, _ = launch(kind, "down", update_volume=True)
    assert result.returncode == 0, result.stderr
    assert operations(calls) == ["version", "guard", "down"]
    assert all("--volumes" not in args and "-v" not in args for args in calls)
    assert browsers == []


@pytest.mark.parametrize("kind", ["bash", "powershell"])
@pytest.mark.parametrize("update_volume", [False, True])
def test_explicit_rebuild_builds_local_source_then_adopts_new_snapshot(
    launch, kind, update_volume
):
    result, calls, _, _ = launch(kind, "rebuild", update_volume=update_volume)
    assert result.returncode == 0, result.stderr
    expected = ["version", "info"]
    if update_volume:
        expected.append("guard")
    assert operations(calls) == expected + ["up", "adopt", "run"]
    assert next(args for args in calls if args[-1] == "adopt")[3:] == [
        "run",
        "--rm",
        "--no-deps",
        "updater",
        "adopt",
    ]
    assert "resume" not in operations(calls)


@pytest.mark.parametrize("kind", ["bash", "powershell"])
@pytest.mark.parametrize("failure", ["up", "adopt"])
def test_rebuild_failure_does_not_adopt_failed_build_or_show_setup(
    launch, kind, failure
):
    result, calls, browsers, _ = launch(kind, "rebuild", fail=failure)
    assert result.returncode != 0
    expected = ["version", "info", "up"]
    if failure == "adopt":
        expected.append("adopt")
    assert operations(calls) == expected
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
    commands = [args for args in calls if args[:2] == ["compose", "-f"]]
    assert len(commands) == 1
    assert commands[0][3:] == expected
    assert not {"--volumes", "-v", "--build"}.intersection(commands[0])
    failed, failed_calls, browsers, _ = launch(kind, action, fail=expected[0])
    assert failed.returncode != 0
    assert len([args for args in failed_calls if args[:2] == ["compose", "-f"]]) == 1
    assert browsers == []


@pytest.mark.parametrize(
    "wrapper,expected,fail",
    [
        ("Start-Bot.ps1", ["version", "info", "up", "adopt", "run"], "up"),
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


@pytest.mark.parametrize("action", ["Start", "Open", "Stop"])
def test_existing_native_environment_remains_on_legacy_path(launch, action):
    result, calls, browsers, _ = launch(
        "powershell", wrapper=f"{action}-Bot.ps1", native=True
    )
    assert result.returncode == 0, result.stderr
    assert calls == []
    assert f"synthetic-native-{action}-NapCat" in result.stdout
    if action in {"Start", "Stop"}:
        assert f"synthetic-native-{action}-LangBot" in result.stdout
        assert browsers == []
    else:
        assert browsers == [["http://127.0.0.1:5300/"]]


def test_native_linux_entrypoint_keeps_original_app_directory(tmp_path):
    if not BASH:
        pytest.skip("Bash is unavailable")
    root = tmp_path / "legacy project with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "Start-LangBot.sh"
    shutil.copyfile(ROOT / "scripts/Start-LangBot.sh", script)
    python = root / "langbot/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$PWD"\nprintf "%s\\n" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    python.chmod(0o755)
    result = subprocess.run(
        [BASH, str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].endswith("/legacy project with spaces/langbot")
    assert lines[1:] == ["-u", "main.py"]
