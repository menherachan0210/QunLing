"""Check publishable working-tree files; report paths, never secret values."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PRIVATE_ROOTS = {".local", ".tools", "langbot", "napcat", "downloads", "packages"}
PRIVATE_NAMES = {"secrets.json", "ai.json", "langbot-admin.json", "setup-state.json"}
PRIVATE_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".lbpkg",
    ".log",
    ".pem",
    ".key",
    ".pfx",
}
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "service credential": re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{35,})\b"
    ),
    "private setup link": re.compile(r"5310/#token=[A-Za-z0-9_-]{30,}"),
}


def inspect(relative, data):
    path = PurePosixPath(relative)
    reasons = []
    if path.parts[0] in PRIVATE_ROOTS or path.name in PRIVATE_NAMES:
        reasons.append("private deployment file")
    if path.suffix.lower() in PRIVATE_SUFFIXES or (
        path.name.startswith(".env")
        and path.name not in {".env.example", ".env.sample"}
    ):
        reasons.append("private/generated file type")
    if len(data) > 5 * 1024 * 1024:
        reasons.append("unexpected large release file")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeError:
        return reasons
    for label, pattern in PATTERNS.items():
        for match in pattern.finditer(content):
            line = content.count("\n", 0, match.start()) + 1
            reasons.append(f"{label} at line {line}")
    return reasons


def main():
    result = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        capture_output=True,
        check=True,
    )
    count = failures = 0
    for raw in sorted(set(result.stdout.split(b"\0")) - {b""}):
        relative = raw.decode("utf-8")
        path = ROOT / relative
        if not path.exists():
            continue
        count += 1
        if path.is_symlink():
            reasons = ["symlink requires release review"]
        elif path.is_file():
            reasons = inspect(relative, path.read_bytes())
        else:
            reasons = ["unexpected directory or submodule"]
        for reason in reasons:
            failures += 1
            print(f"{relative}: {reason}")
    print(
        f"Checked {count} publishable files; {failures} findings. Secret values are never printed."
    )
    print(
        "This checks the working tree, not Git history or every possible credential format."
    )
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
