"""Install pinned file-task dependencies and expose the public shared package."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]


def pins(root=ROOT):
    entries = []
    for line in (root / "qunling/requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.count("==") != 1 or any(c in line for c in "/\\;@ \t"):
            raise ValueError("File runtime requirements must use exact package==version pins")
        entries.append(tuple(line.split("==")))
    if not entries:
        raise ValueError("File runtime dependency pins are missing")
    return entries


def missing_requirements(root=ROOT):
    missing = []
    for name, version in pins(root):
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = None
        if actual != version:
            missing.append(f"{name}=={version}")
    return missing


def register_package(root=ROOT):
    # A path-only .pth works for -I child processes as well as ordinary imports.
    # The trusted project root contains only public code; no credentials are read.
    if not (root / "qunling/__init__.py").is_file():
        raise ValueError("Shared QunLing source package is missing")
    target = Path(sysconfig.get_path("purelib")) / "qunling-local.pth"
    value = str(root.resolve()) + "\n"
    if not target.exists() or target.read_text(encoding="utf-8") != value:
        target.write_text(value, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check without installing or writing")
    args = parser.parse_args(argv)
    missing = missing_requirements()
    if args.check:
        if missing:
            raise SystemExit("Missing pinned file runtime dependencies: " + ", ".join(missing))
        print("Pinned file runtime dependencies verified.")
        return
    if missing:
        try:
            import pip  # noqa: F401
        except ImportError:
            subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"], check=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                        "-r", str(ROOT / "qunling/requirements.txt")], check=True)
    register_package()
    platform = "windows-x64" if os.name == "nt" else "linux-x64"
    decoder = ROOT / ".local/tools/7zip" / platform / ("7z.exe" if os.name == "nt" else "7zz")
    if not decoder.is_file() and not (shutil.which("7zz") or shutil.which("7z")):
        subprocess.run([sys.executable, str(ROOT / "scripts/Setup-ArchiveTools.py"),
                        "--platform", platform], check=True)
    print("Pinned file runtime dependencies and shared package are ready.")


if __name__ == "__main__":
    main()
