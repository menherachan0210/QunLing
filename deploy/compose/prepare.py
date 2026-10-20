"""Build only public, pinned sources; do not read workstation runtime state."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
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


def extract_tree(raw: bytes, destination: Path, *, prefix: str = "") -> int:
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
                or stat.S_ISLNK(member.external_attr >> 16)
            ):
                raise ValueError("Unsafe entry in public source archive")
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
    extract_tree(download_verified(sdk), sdk_source, prefix=sdk["prefix"])
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


def package_metadata(package: Path) -> dict:
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
            subprocess.run(
                [sys.executable, "-m", "langbot_plugin.cli", "build", "-o", str(built)],
                cwd=staging,
                check=True,
            )
            artifacts = list(built.glob("*.lbpkg"))
            if len(artifacts) != 1:
                raise ValueError("Plugin builder did not produce exactly one artifact")
            metadata = package_metadata(artifacts[0])
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
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

    # Chroma verifies its pinned SHA-256 before extracting the public ONNX model.
    # Keeping the default root user's cache in this image avoids first-use setup.
    embedding = ONNXMiniLM_L6_V2()
    embedding._download_model_if_not_exists()
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
