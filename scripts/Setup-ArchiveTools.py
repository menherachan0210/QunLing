"""Install verified official 7-Zip CLI privately, on Windows or Linux."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = "26.04"
BASE = "https://github.com/ip7z/7zip/releases/download/" + VERSION + "/"
# Digests published by the official ip7z/7zip release, pinned for reproducibility.
ASSETS = {
    "windows-x64": (
        "7z2604-x64.exe",
        "d54bf805f9f3704d1e8db2fa3498ae7ef2df0312b40b558e7c71c734430a665d",
    ),
    "windows-arm64": (
        "7z2604-arm64.exe",
        "d4117b95495b0e925334ae7b5bddf2853513274603de15dee6acf03999fbc09c",
    ),
    "linux-x64": (
        "7z2604-linux-x64.tar.xz",
        "fc0327ba27e89bd086cf426dff17d77de582953cdbbc10a6576540a06853ffcd",
    ),
    "linux-arm64": (
        "7z2604-linux-arm64.tar.xz",
        "5b0ac3aa91c1e3499f011d6af69d7f6b4de3ef2f011d9e2ff44c58ebd04aa388",
    ),
}
BOOTSTRAP = (
    "7zr.exe",
    "256feca8e274e5da655e2a284fabafd9f554365eb164862089dacd4e8276d282",
)


def host_tag():
    system = platform.system().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower()
    )
    tag = f"{system}-{arch}"
    if tag not in ASSETS:
        raise RuntimeError(
            "Supported targets: Windows/Linux x64 or ARM64. Configure a trusted system 7zz otherwise."
        )
    return tag


def download(asset, folder):
    name, expected = asset
    path = folder / name
    cache = ROOT / ".local" / "tools" / "7zip-bootstrap" / name
    if cache.is_file() and hashlib.sha256(cache.read_bytes()).hexdigest() == expected:
        shutil.copyfile(cache, path)
        return path
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        BASE + name, headers={"User-Agent": "QQComicTranslator-Setup/1.0"}
    )
    with opener.open(request, timeout=60) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected:
        raise RuntimeError(
            "Official archive checksum mismatch; nothing was executed or installed."
        )
    path.write_bytes(raw)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--platform",
        choices=ASSETS,
        help="Prepare another Linux architecture without executing it",
    )
    args = parser.parse_args()
    tag = args.platform or host_tag()
    if tag.startswith("windows") and os.name != "nt":
        raise RuntimeError(
            "Prepare Windows binaries on Windows; Linux deployment uses native 7zz."
        )
    target = ROOT / ".local" / "tools" / "7zip" / tag
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="7zip-setup-", dir=target.parent) as temp:
        temp = Path(temp)
        archive = download(ASSETS[tag], temp)
        prepared = temp / "prepared"
        prepared.mkdir()
        if tag.startswith("windows"):
            bootstrap = download(BOOTSTRAP, temp)
            command = [
                str(bootstrap),
                "x",
                str(archive),
                "-y",
                "-o" + str(prepared),
                "7z.exe",
                "7z.dll",
                "License.txt",
                "readme.txt",
                "History.txt",
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=90,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
            if (
                result.returncode != 0
                or not (prepared / "7z.exe").is_file()
                or not (prepared / "7z.dll").is_file()
            ):
                raise RuntimeError(
                    "Verified installer extraction failed; no system installer was run."
                )
            executable = "7z.exe"
        else:
            allowed = {"7zz", "License.txt", "readme.txt", "History.txt"}
            with tarfile.open(archive, "r:xz") as source:
                for member in source.getmembers():
                    name = Path(member.name).name
                    if name not in allowed or not member.isfile():
                        continue
                    if not 0 < member.size <= 16 * 1024 * 1024:
                        raise RuntimeError("Unexpected official release member size")
                    stream = source.extractfile(member)
                    if stream is not None:
                        (prepared / name).write_bytes(stream.read())
            executable = "7zz"
            if not (prepared / executable).is_file():
                raise RuntimeError("Official archive did not contain 7zz")
            (prepared / executable).chmod(0o755)
        if not (prepared / "License.txt").is_file():
            raise RuntimeError(
                "Official license is missing; incomplete installation refused"
            )
        target.mkdir(parents=True, exist_ok=True)
        for file in prepared.iterdir():
            shutil.copy2(file, target / file.name)
        metadata = {
            "version": VERSION,
            "platform": tag,
            "source": BASE + ASSETS[tag][0],
            "sha256": ASSETS[tag][1],
            "executable": str(target / executable),
        }
        (target / "installation.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )
    print("Private archive tool installed: " + str(target / executable))
    print("No system file associations or global PATH were changed.")


if __name__ == "__main__":
    main()
