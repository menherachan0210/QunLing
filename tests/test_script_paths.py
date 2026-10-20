"""Relocated tools keep project state outside scripts and work from another cwd."""

import ast
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "name,variable,relative",
    [
        ("Apply-RuntimePatches.py", "BASE_DIR", "."),
        ("Check-Release.py", "ROOT", "."),
        ("Setup-ArchiveTools.py", "ROOT", "."),
        ("local_api.py", "ROOT", "."),
        ("chat_style.py", "STYLE_PATH", "config/chat-style.txt"),
        ("Verify-GroupContext.py", "ROOT", "."),
        ("Verify-GroupConversation.py", "ROOT", "."),
        ("Verify-MultiImage.py", "ROOT", "."),
        ("Verify-WebSearch.py", "ROOT", "."),
    ],
)
def test_root_expressions_resolve_from_script_location(
    tmp_path, monkeypatch, name, variable, relative
):
    # Evaluate only path declarations; integration scripts must not call APIs.
    monkeypatch.chdir(tmp_path)
    script = ROOT / "scripts" / name
    tree = ast.parse(script.read_text(encoding="utf-8"))
    value = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == variable
            for target in node.targets
        )
    )
    result = eval(
        compile(ast.Expression(value), str(script), "eval"),
        {"Path": Path, "__file__": str(script)},
    )
    assert result == ROOT / relative


def test_shared_style_can_load_after_copy_from_unrelated_directory(tmp_path):
    project = tmp_path / "project with spaces"
    (project / "scripts").mkdir(parents=True)
    (project / "config").mkdir()
    shutil.copyfile(ROOT / "scripts/chat_style.py", project / "scripts/chat_style.py")
    (project / "config/chat-style.txt").write_text("synthetic style", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from scripts.chat_style import styled_prompts; "
            "assert styled_prompts([])[0]['content'].endswith('synthetic style')",
            str(project),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("action", ["Start", "Open", "Stop"])
def test_windows_root_entrypoints_quote_relocated_script(action):
    text = (ROOT / f"{action}-Bot.cmd").read_text()
    assert f'"%~dp0scripts\\{action}-Bot.ps1"' in text
    assert "if errorlevel 1" in text
    assert not (ROOT / f"{action}-Bot.ps1").exists()


def test_release_scanner_points_to_project_not_scripts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spec = importlib.util.spec_from_file_location(
        "relocated_release_check", ROOT / "scripts/Check-Release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.ROOT == ROOT
    assert module.inspect(".local/secrets.json", b"{}") == ["private deployment file"]
