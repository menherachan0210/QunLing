"""Consistent, verified backups; invoked in an isolated named-volume helper."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile

from deploy.updater.engine import UpdateError, read, write


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def validate_archive(path):
    with tarfile.open(path, "r:") as archive:
        members = archive.getmembers()
        def normalized(value):
            name = PurePosixPath(value)
            if (name.is_absolute() or ".." in name.parts or "\\" in value
                    or any(ord(character) < 32 for character in value) or str(name) in {"", "."}):
                raise UpdateError("备份包含非法文件路径。")
            return str(name)
        names = [normalized(m.name) for m in members]
        if len(set(names)) != len(names):
            raise UpdateError("备份包含重复文件路径。")
        links = {normalized(m.name) for m in members if m.issym()}
        by_name = {normalized(m.name): m for m in members}
        for member in members:
            name = PurePosixPath(normalized(member.name))
            if name.is_absolute() or ".." in name.parts or member.isdev() or member.isfifo():
                raise UpdateError("备份包含非法文件路径或设备节点。")
            if any(str(parent) in links for parent in name.parents):
                raise UpdateError("备份文件不能写入符号链接的子路径。")
            if member.islnk() and (
                normalized(member.linkname) not in by_name or not by_name[normalized(member.linkname)].isreg()
            ):
                raise UpdateError("备份包含非法硬链接。")
        return members


def backup(data, destination):
    data, destination = Path(data), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".partial")
    # A full tar read and checksum must succeed before publishing its manifest.
    with tarfile.open(temporary, "w:", dereference=False) as archive:
        for child in sorted(data.iterdir()):
            archive.add(child, arcname=child.name, recursive=True)
    validate_archive(temporary)
    checksum = digest(temporary)
    os.replace(temporary, destination)
    metadata = data.stat()
    write(str(destination) + ".json", {
        "sha256": checksum, "size": destination.stat().st_size,
        "root_mode": stat.S_IMODE(metadata.st_mode),
        "root_uid": metadata.st_uid, "root_gid": metadata.st_gid,
    })


def verify(destination):
    destination = Path(destination)
    manifest = read(str(destination) + ".json", {})
    if (not destination.is_file() or manifest.get("size") != destination.stat().st_size
            or manifest.get("sha256") != digest(destination)):
        raise UpdateError("备份校验失败，未覆盖数据。")
    validate_archive(destination)


def restore(data, destination):
    data, destination = Path(data), Path(destination)
    verify(destination)  # Complete all validation before deleting any data.
    if not data.is_dir() or data.is_symlink():
        raise UpdateError("恢复目标不是有效的数据目录。")
    for child in data.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    with tarfile.open(destination, "r:") as archive:
        # The archive is created by this helper, hash checked, and all member
        # paths/link ancestry checked above. Preserve ownership and venv links.
        archive.extractall(data, filter="fully_trusted")
    metadata = read(str(destination) + ".json")
    if "root_mode" in metadata:
        os.chmod(data, metadata["root_mode"])
        if hasattr(os, "chown"):
            os.chown(data, metadata["root_uid"], metadata["root_gid"])
