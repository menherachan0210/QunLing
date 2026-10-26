"""Build only public, pinned sources; do not read workstation runtime state."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[2]
LOCK = Path(__file__).with_name("sources.lock.json")
PLUGIN_NAMES = (
    "LocalAgent",
    "LongTermMemory",
    "AIImagePlugin",
    "QQTextReader",
    "QQComicTranslator",
    "QQGroupContext",
    "QQWebSearch",
)
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "data",
}
LOCAL_AGENT_PATCH_TARGETS = {
    "pkg/agent_core/langbot.py", "pkg/run_assembly.py", "pkg/task_continuation.py",
}


def apply_local_agent_patch(staging: Path, root: Path):
    for name, targets in (
        ("localagent-task-continuation.patch", LOCAL_AGENT_PATCH_TARGETS),
        ("localagent-dialogue-context.patch", {"pkg/context_pipeline.py"}),
    ):
        _apply_local_agent_patch(staging, root, name, targets)


def _apply_local_agent_patch(staging: Path, root: Path, name: str, targets: set[str]):
    """Patch only an isolated, pinned source staging tree; never runtime caches."""
    import yaml

    metadata = yaml.safe_load((staging / "manifest.yaml").read_text(encoding="utf-8"))["metadata"]
    expected = json.loads(LOCK.read_text(encoding="utf-8"))["upstream_plugins"]["local_agent_version"]
    if (metadata.get("author"), metadata.get("name"), str(metadata.get("version"))) != (
        "langbot-team", "LocalAgent", expected
    ):
        raise ValueError("LocalAgent continuation patch requires its pinned upstream version")
    patch = (root / "patches" / name).read_text(encoding="utf-8")
    patch = patch.replace("\r\n", "\n")
    paths = []
    for block in re.split(r"(?=^diff --git )", patch, flags=re.M):
        if not block.strip():
            continue
        match = re.match(r"diff --git a/(\S+) b/(\S+)\n", block)
        if not match or match[1] != match[2] or match[1] not in targets:
            raise ValueError("Unexpected LocalAgent continuation patch target")
        path = match[1]
        if path in paths:
            raise ValueError("Duplicate LocalAgent patch target")
        paths.append(path)
        target = staging / path
        if not target.resolve().is_relative_to(staging.resolve()) or target.is_symlink():
            raise ValueError("Redirected LocalAgent patch target")
        new_file = path == "pkg/task_continuation.py"
        before = "--- /dev/null\n" if new_file else f"--- a/{path}\n"
        if before not in block or f"+++ b/{path}\n" not in block:
            raise ValueError("Invalid LocalAgent patch source header")
        if not new_file and not target.is_file():
            raise ValueError("Pinned LocalAgent source file is missing")
        if re.search(r"^(?:rename |copy |GIT binary patch|deleted file mode)", block, re.M):
            raise ValueError("Unsupported LocalAgent patch operation")
    if set(paths) != targets:
        raise ValueError("Incomplete LocalAgent continuation patch")
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX"):
        env.pop(key, None)
    env["GIT_CEILING_DIRECTORIES"] = str(staging.resolve().parent)
    def apply(*options):
        return subprocess.run(
            ["git", "apply", "--whitespace=nowarn", *options, "-"], cwd=staging,
            input=patch.encode("utf-8"), capture_output=True, timeout=30, env=env,
        )
    if apply("--reverse", "--check").returncode == 0:
        return
    if apply("--check").returncode or apply().returncode or apply("--reverse", "--check").returncode:
        raise ValueError("LocalAgent source does not safely match the pinned continuation patch")


def download_verified(asset: dict, *, opener=None) -> bytes:
    size = asset["size"]
    if type(size) is not int or not 0 < size <= 64 * 1024 * 1024:
        raise ValueError("Unexpected pinned source size")
    request = urllib.request.Request(
        asset["url"], headers={"User-Agent": "qq-ai-bot-public-build/1"}
    )
    opener = opener or urllib.request.urlopen
    with opener(request, timeout=60) as response:
        raw = response.read(size + 1)
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != asset["sha256"]:
        raise ValueError("Public source size or SHA-256 mismatch; build stopped")
    return raw


def extract_tree(
    raw: bytes,
    destination: Path,
    *,
    prefix: str = "",
    document_links: dict[str, str] | None = None,
) -> int:
    """Preflight a verified ZIP before extracting regular files to a new tree."""
    if destination.exists():
        raise ValueError("Source destination already exists; refusing to overwrite")
    selected = []
    names = set()
    total = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if len(archive.infolist()) > 30_000:
            raise ValueError("Too many files in public source archive")
        for member in archive.infolist():
            raw_name = member.orig_filename
            if not raw_name.startswith(prefix):
                continue
            relative = raw_name[len(prefix) :].rstrip("/")
            if not relative:
                continue
            path = PurePosixPath(relative)
            if (
                path.is_absolute()
                or any(part in {"", ".", ".."} for part in relative.split("/"))
                or any(char in relative for char in "\\:\x00")
            ):
                raise ValueError("Unsafe entry in public source archive")
            if stat.S_ISLNK(member.external_attr >> 16):
                target = (document_links or {}).get(relative)
                if (
                    not isinstance(target, str)
                    or not target
                    or PurePosixPath(target).is_absolute()
                    or any(
                        part in {"", ".", "..", ".git"} for part in target.split("/")
                    )
                    or any(char in target for char in "\\:\x00")
                    or member.file_size > 4096
                    or archive.read(member) != target.encode("utf-8")
                ):
                    raise ValueError("Unsafe entry in public source archive")
                try:
                    target_member = archive.getinfo(prefix + target)
                except KeyError:
                    raise ValueError("Pinned document link target is missing") from None
                if (
                    target_member.is_dir()
                    or stat.S_IFMT(target_member.external_attr >> 16)
                    not in {0, stat.S_IFREG}
                    or ".git" in path.parts
                ):
                    raise ValueError(
                        "Pinned document link must reference a regular file"
                    )
                # Materialize the explicitly pinned alias as a normal file. Its
                # own path is preserved; no filesystem symlink is ever created.
                member = target_member
            if ".git" in path.parts or member.is_dir():
                continue
            if relative in names:
                raise ValueError("Duplicate entry in public source archive")
            names.add(relative)
            total += member.file_size
            if member.file_size > 64 * 1024 * 1024 or total > 256 * 1024 * 1024:
                raise ValueError("Public source archive exceeds extraction limits")
            selected.append((member, path))
        if not selected:
            raise ValueError("Pinned source subtree is missing")
        destination.mkdir(parents=True)
        for member, path in selected:
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
            target.chmod(0o755 if member.external_attr >> 16 & 0o111 else 0o644)
    return len(selected)


def prepare_sources(root: Path):
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    raw = download_verified(lock["langbot"])
    extract_tree(raw, root / "langbot")
    frontend = root / "langbot/web/dist/index.html"
    if not frontend.is_file():
        raise ValueError("The pinned release is missing its compiled WebUI")
    raw = download_verified(lock["upstream_plugins"])
    extract_tree(
        raw,
        root / "custom-plugins/LocalAgent",
        prefix=lock["upstream_plugins"]["local_agent_prefix"],
    )
    # Retain the upstream repository license with its extracted LocalAgent.
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        commit = lock["upstream_plugins"]["commit"]
        for name in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
            path = f"langbot-plugins-{commit}/{name}"
            if path in archive.namelist():
                target = root / "custom-plugins/LocalAgent" / ("UPSTREAM_" + name)
                target.write_bytes(archive.read(path))
    sdk = lock["plugin_sdk_source"]
    sdk_source = root / "upstream/langbot-plugin-sdk"
    extract_tree(
        download_verified(sdk),
        sdk_source,
        prefix=sdk["prefix"],
        # Upstream v0.7.11 contains this documentation alias. Its bytes and
        # target are checked above; all unlisted archive links remain rejected.
        document_links={"CLAUDE.md": "AGENTS.md"},
    )
    for required in (
        "LICENSE",
        "README.md",
        "pyproject.toml",
        "src/langbot_plugin/runtime/LICENSE",
    ):
        if not (sdk_source / required).is_file():
            raise ValueError(
                "Pinned SDK/Runtime source is missing its source or license metadata"
            )
    print(
        "Verified LangBot, WebUI, LocalAgent and complete SDK/Runtime source prepared."
    )


def package_metadata(package: Path, *, source: Path | None = None) -> dict:
    import yaml

    with zipfile.ZipFile(package) as archive:
        for name in archive.namelist():
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or EXCLUDED_PARTS.intersection(path.parts)
                or path.name == ".env"
                or path.name.startswith(".env.")
                or path.suffix in {".pyc", ".log", ".lbpkg"}
            ):
                raise ValueError("A package contains excluded runtime/build files")
        metadata = yaml.safe_load(archive.read("manifest.yaml"))["metadata"]
        if source is not None:
            # The SDK logs individual ZIP failures but still exits successfully.
            # Verify executable sources and tool manifests before publishing.
            required = [source / "main.py", source / "requirements.txt"]
            for directory in ("pkg", "components", "qunling"):
                required.extend(p for p in (source / directory).rglob("*") if p.is_file())
            for path in required:
                if not path.is_file():
                    continue
                name = path.relative_to(source).as_posix()
                if name not in archive.namelist() or archive.read(name) != path.read_bytes():
                    raise ValueError(f"Plugin package source missing or changed: {name}")
    return {
        "filename": package.name,
        "identity": metadata["author"] + "/" + metadata["name"],
        "version": metadata["version"],
        "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
    }


def build_packages(root: Path):
    output = root / "packages"
    if output.exists():
        raise ValueError("Package output already exists; refusing stale artifacts")
    output.mkdir()
    index = []
    for name in PLUGIN_NAMES:
        source = root / "custom-plugins" / name
        if not (source / "manifest.yaml").is_file():
            raise ValueError(f"Missing distributable plugin source: {name}")
        with tempfile.TemporaryDirectory(prefix="package-") as temporary:
            temporary = Path(temporary)
            staging = temporary / name
            built = temporary / "output"
            built.mkdir()

            def exclude(directory, entries):
                return [
                    entry
                    for entry in entries
                    if entry in EXCLUDED_PARTS | {"tests"}
                    or entry == ".env"
                    or entry.startswith(".env.")
                    or Path(entry).suffix in {".pyc", ".log", ".lbpkg"}
                    or (Path(directory) / entry).is_symlink()
                ]

            shutil.copytree(source, staging, ignore=exclude)
            if name == "LocalAgent":
                apply_local_agent_patch(staging, root)
            shared = root / "qunling"
            if not (shared / "__init__.py").is_file():
                raise ValueError("Shared QunLing source package is missing")
            # Each plugin has an isolated import path. Include the same public
            # sources without leaking host state or creating private parser envs.
            shutil.copytree(shared, staging / "qunling", ignore=exclude)
            for notice in ("LICENSE", "NOTICE"):
                if not (root / notice).is_file():
                    raise ValueError("Shared package license metadata is missing")
                shutil.copyfile(root / notice, staging / "qunling" / notice)
            # Reproducible source archives may use epoch-zero timestamps, which
            # the SDK's ZIP writer cannot represent. Only normalize staging.
            for path in staging.rglob("*"):
                if path.is_file() and path.stat().st_mtime < 315532800:
                    os.utime(path, (946684800, 946684800))
            subprocess.run(
                [sys.executable, "-m", "langbot_plugin.cli", "build", "-o", str(built)],
                cwd=staging,
                check=True,
            )
            artifacts = list(built.glob("*.lbpkg"))
            if len(artifacts) != 1:
                raise ValueError("Plugin builder did not produce exactly one artifact")
            metadata = package_metadata(artifacts[0], source=staging)
            if metadata["identity"].split("/")[-1] != name:
                raise ValueError("Plugin package identity mismatch")
            target = output / artifacts[0].name
            shutil.copyfile(artifacts[0], target)
            index.append(metadata)
    expected = json.loads(LOCK.read_text(encoding="utf-8"))["upstream_plugins"][
        "local_agent_version"
    ]
    if (
        next(item for item in index if item["identity"].endswith("/LocalAgent"))[
            "version"
        ]
        != expected
    ):
        raise ValueError("Pinned LocalAgent version mismatch")
    (output / "packages.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("Prepared seven plugin packages and packages.json.")


def prepare_embedding():
    sys.path.insert(0, str(ROOT))
    from qunling.embeddings import prepare

    prepare()
    print("Verified local memory embedding model is available.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("sources", "packages", "embedding"))
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    {
        "sources": lambda: prepare_sources(args.root),
        "packages": lambda: build_packages(args.root),
        "embedding": prepare_embedding,
    }[args.action]()


if __name__ == "__main__":
    main()
