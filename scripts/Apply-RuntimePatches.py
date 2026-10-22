#!/usr/bin/env python3
"""Check or apply the pinned LangBot 4.11.0 / plugin SDK 0.7.11 patches.

The default operation is read-only. Stop LangBot before using --apply, then
restart it with the same Python interpreter. Plugin source patches and patch
test files are deliberately excluded from this runtime-only installer.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from difflib import unified_diff
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tomllib


BASE_DIR = Path(__file__).resolve().parents[1]
SDK_PREFIX = ".venv/Lib/site-packages/"
PATCHES = (
    ("host-managed-runtime-transport.patch", "host", False),
    ("host-plugin-health.patch", "host", False),
    ("host-persisted-plugin-installations.patch", "host", False),
    ("host-tool-password-redaction.patch", "host", False),
    ("host-proactive-chat.patch", "host", False),
    ("host-proactive-context.patch", "host", False),
    ("host-qq-status.patch", "host", False),
    ("host-qq-member-sessions.patch", "host", False),
    ("onebot-group-trigger.patch", "host", False),
    ("chroma-delete-count.patch", "host", False),
    ("onebot-image-vision.patch", "host", False),
    ("image-tool-host-timeout.patch", "host", False),
    ("onebot-text-attachment.patch", "host", False),
    ("onebot-group-context.patch", "host", False),
    ("onebot-group-information.patch", "host", False),
    ("onebot-forward-placeholder.patch", "host", False),
    ("group-message-isolation.patch", "host", False),
    ("group-history-speakers.patch", "host", False),
    ("file-outbox-host.patch", "host", False),
    ("host-qq-dispatch.patch", "host", False),
    ("sdk-image-tool-timeout.patch", "sdk", False),
    ("sdk-dedicated-background.patch", "sdk", False),
    ("sdk-windows-dependency-temp.patch", "sdk", True),
    ("sdk-attachment-message-budget.patch", "sdk", False),
    ("sdk-tool-provenance.patch", "sdk", False),
    ("sdk-qunling-worker-storage.patch", "sdk", False),
    ("sdk-transport-recovery.patch", "sdk", False),
)


class PatchError(RuntimeError):
    """A patch cannot safely be checked or applied to this installation."""


@dataclass(frozen=True)
class PatchPlan:
    name: str
    root: Path
    payload: bytes
    state: str
    upgrade_from: bytes | None = None
    verify_payload: bytes | None = None


def inspect_python(python: Path) -> dict:
    """Ask the target interpreter for its installation paths and SDK version."""
    probe = """
import importlib.metadata, importlib.util, json, pathlib, sys, sysconfig
spec = importlib.util.find_spec('langbot_plugin')
print(json.dumps({
    'platform': sys.platform,
    'purelib': sysconfig.get_path('purelib'),
    'sdk_version': importlib.metadata.version('langbot-plugin'),
    'sdk_root': str(pathlib.Path(spec.origin).resolve().parent) if spec and spec.origin else None,
}))
"""
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", probe],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        info = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise PatchError(
            "Cannot inspect the target Python; install langbot-plugin==0.7.11 "
            "in that environment first."
        ) from exc
    if info.get("sdk_version") != "0.7.11":
        raise PatchError("These SDK patches require langbot-plugin==0.7.11.")
    purelib = Path(info["purelib"]).resolve()
    sdk_root = info.get("sdk_root")
    if not sdk_root or Path(sdk_root).resolve() != purelib / "langbot_plugin":
        raise PatchError(
            "The SDK is not installed in the target Python's purelib directory."
        )
    return info


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in value.split("/"))
        or any(char in value for char in "\\\x00\r\n\t:")
    ):
        raise PatchError(f"Unsupported patch path: {value!r}")
    return value


def runtime_payload(source: str, component: str) -> tuple[bytes, tuple[str, ...]]:
    """Select runtime source blocks and remap SDK paths relative to purelib."""
    if component not in ("host", "sdk"):
        raise PatchError("Unknown patch component.")
    source = source.replace("\r\n", "\n")
    git_style = source.startswith("diff --git ")
    delimiter = r"(?=^diff --git )" if git_style else r"(?=^--- (?:a/|/dev/null))"
    blocks = re.split(delimiter, source, flags=re.M)
    selected: list[str] = []
    paths: list[str] = []
    for block in blocks:
        if not block.strip():
            continue
        lines = block.splitlines(keepends=True)
        if git_style:
            header = re.fullmatch(r"diff --git a/(\S+) b/(\S+)\n?", lines[0])
            if not header or header.group(1) != header.group(2):
                raise PatchError(
                    "Only ordinary, non-renamed unified patch files are supported."
                )
            original = safe_path(header.group(1))
        else:
            if (
                len(lines) < 3
                or not lines[0].startswith("--- ")
                or not lines[1].startswith("+++ b/")
            ):
                raise PatchError("Invalid unified patch header.")
            original = safe_path(lines[1][6:].rstrip("\n"))
            lines.insert(0, f"diff --git a/{original} b/{original}\n")
        if original.startswith("tests/"):
            continue
        prefixes = (("src/langbot/",) if component == "host"
                    else (SDK_PREFIX + "langbot_plugin/", "langbot_plugin/"))
        if not original.startswith(prefixes):
            raise PatchError(f"Unexpected {component} patch target: {original}")
        mapped = original if component == "host" else original.removeprefix(SDK_PREFIX)
        safe_path(mapped)
        before = f"--- a/{original}\n"
        after = f"+++ b/{original}\n"
        if lines.count(before) != 1 or lines.count(after) != 1:
            raise PatchError("Runtime patches must modify existing text files.")
        if any(
            line.startswith(
                (
                    "rename ",
                    "copy ",
                    "GIT binary patch",
                    "new file mode",
                    "deleted file mode",
                )
            )
            for line in lines
        ):
            raise PatchError("Unsupported runtime patch operation.")
        lines[0] = f"diff --git a/{mapped} b/{mapped}\n"
        lines[lines.index(before)] = f"--- a/{mapped}\n"
        lines[lines.index(after)] = f"+++ b/{mapped}\n"
        selected.append("".join(lines))
        paths.append(mapped)
    if not selected:
        raise PatchError("Patch contains no supported runtime source files.")
    return "".join(selected).encode("utf-8"), tuple(paths)


def check_targets(root: Path, paths: tuple[str, ...]) -> None:
    resolved_root = root.resolve()
    for path in paths:
        target = root / path
        if not target.is_file() or not target.resolve().is_relative_to(resolved_root):
            raise PatchError(f"Missing or redirected runtime source file: {path}")


def git_apply(root: Path, payload: bytes, *options: str) -> subprocess.CompletedProcess:
    # A parent checkout would make git silently skip paths outside its prefix.
    # Keep apply rooted at this directory, even when purelib is inside a repo.
    env = os.environ.copy()
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_PREFIX",
    ):
        env.pop(name, None)
    env["GIT_CEILING_DIRECTORIES"] = str(root.resolve().parent)
    try:
        return subprocess.run(
            ["git", "apply", "--whitespace=nowarn", *options, "-"],
            cwd=root,
            input=payload,
            capture_output=True,
            timeout=30,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PatchError("Git could not check/apply a runtime patch.") from exc


def proactive_reply_upgrade(name: str, root: Path, payload: bytes, component: str) -> PatchPlan | None:
    """Upgrade exactly the previously released complete proactive Host patch.

    Never treat a matching call line alone as an installed patch. Both this
    known payload and every legacy hunk must match before a one-line delta is
    even proposed. Other patch names/versions retain the normal strict checks.
    """
    if component != "host":
        return None
    if name == "host-proactive-chat.patch":
        path = "src/langbot/pkg/platform/adapters/aiocqhttp/api_impl.py"
        old = "            proactive_sent(self, native.group_id, message, raw, str(native.user_id))\n"
        new = old[:-2] + ", reply_to=str(native.message_id) if getattr(native, 'message_id', None) is not None else None)\n"
        previous_hash = "730713902613a135d25330a71ac54e5ba80e044f6bd20a399804f099b5043f0c"
    elif name == "host-qq-dispatch.patch":
        path = "src/langbot/pkg/provider/tools/toolmgr.py"
        old = "            result = await invoke_tool(self.ap, query, invoke, name)\n"
        new = "            result = await invoke_tool(self.ap, query, invoke, name, source=source)\n"
        previous_hash = "eb9ae5e935be58d3e2804d6f61007203756dd21849646d89bbe524f88c28e0da"
    else:
        return None
    new_added = ("+" + new).encode("utf-8")
    if payload.count(new_added) != 1:
        return None
    previous = payload.replace(new_added, ("+" + old).encode("utf-8"), 1)
    if hashlib.sha256(previous).hexdigest() != previous_hash:
        return None
    if git_apply(root, previous, "--reverse", "--check").returncode != 0:
        return None
    original = (root / path).read_text(encoding="utf-8")
    if original.count(old) != 1:
        return None
    updated = original.replace(old, new, 1)
    delta = "".join(unified_diff(original.splitlines(keepends=True), updated.splitlines(keepends=True),
                                fromfile=f"a/{path}", tofile=f"b/{path}")).encode("utf-8")
    if not delta or git_apply(root, delta, "--check").returncode != 0:
        return None
    return PatchPlan(name, root, delta, "ready", upgrade_from=previous, verify_payload=payload)


def build_plan(name: str, root: Path, source: str, component: str) -> PatchPlan:
    payload, paths = runtime_payload(source, component)
    check_targets(root, paths)
    if git_apply(root, payload, "--reverse", "--check").returncode == 0:
        state = "already-applied"
    elif git_apply(root, payload, "--check").returncode == 0:
        state = "ready"
    else:
        upgrade = proactive_reply_upgrade(name, root, payload, component)
        if upgrade is not None:
            return upgrade
        raise PatchError(
            f"{name}: source does not match either the original or patched version; "
            "nothing was overwritten. Review the installed version and local changes."
        )
    return PatchPlan(name, root, payload, state)


def execute_plans(plans: list[PatchPlan], apply: bool) -> None:
    """Validate the whole set before any writes, then apply only ready patches."""
    for plan in plans:
        print(f"{plan.state}: {plan.name}")
    if not apply:
        print(
            "Check only; no files changed. Use --apply with LangBot stopped to apply ready patches."
        )
        return
    for plan in plans:
        if plan.state != "ready":
            continue
        if (plan.upgrade_from is not None
                and git_apply(plan.root, plan.upgrade_from, "--reverse", "--check").returncode != 0):
            raise PatchError(
                f"{plan.name}: complete legacy patch changed after preflight; stopped without applying this patch."
            )
        if git_apply(plan.root, plan.payload, "--check").returncode != 0:
            raise PatchError(
                f"{plan.name}: files changed after preflight; stopped without applying this patch."
            )
        if git_apply(plan.root, plan.payload).returncode != 0:
            raise PatchError(
                f"{plan.name}: Git refused to apply the patch; no reject/overwrite mode was used."
            )
        if git_apply(plan.root, plan.verify_payload or plan.payload, "--reverse", "--check").returncode != 0:
            raise PatchError(f"{plan.name}: post-apply verification failed.")
        print(f"applied: {plan.name}")
    print("Runtime patches verified. Restart LangBot with the same Python environment.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--langbot",
        type=Path,
        default=BASE_DIR / "langbot",
        help="LangBot 4.11.0 source directory",
    )
    parser.add_argument(
        "--python",
        type=Path,
        help="Target Python executable (defaults to LangBot's .venv)",
    )
    parser.add_argument("--component", choices=("all", "host", "sdk"), default="all")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply matching patches; default is check-only",
    )
    args = parser.parse_args(argv)
    try:
        if shutil.which("git") is None:
            raise PatchError("Git is required; install it before checking patches.")
        langbot = args.langbot.resolve()
        project = tomllib.loads(
            (langbot / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        if project.get("name") != "langbot" or project.get("version") != "4.11.0":
            raise PatchError(
                "These Host patches require the LangBot 4.11.0 source tree."
            )
        info = None
        if args.component in ("all", "sdk"):
            python = args.python or langbot / ".venv" / (
                "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
            )
            # A POSIX venv executable is normally a symlink to the base Python.
            # Keep its entry path so Python finds the venv's pyvenv.cfg/site-packages.
            info = inspect_python(python.absolute())
            print(
                f"SDK target: {info['purelib']} (langbot-plugin {info['sdk_version']})"
            )
        plans = []
        for name, component, windows_only in PATCHES:
            if args.component not in ("all", component):
                continue
            if windows_only and info["platform"] != "win32":
                print(f"skipped (Windows only): {name}")
                continue
            root = langbot if component == "host" else Path(info["purelib"]).resolve()
            source = (BASE_DIR / "patches" / name).read_text(encoding="utf-8")
            plans.append(build_plan(name, root, source, component))
        execute_plans(plans, args.apply)
        return 0
    except (PatchError, OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
