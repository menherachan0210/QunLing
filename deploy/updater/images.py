"""Resolve commit-tagged GHCR images to immutable, verified deployment digests."""
from __future__ import annotations

import json
import re


def pull_prebuilt(engine, revision, log):
    from deploy.updater.engine import UpdateError
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise UpdateError("预构建镜像需要完整提交编号。")
    result = []
    for role in ("app", "updater"):
        repository = "ghcr.io/" + engine.repository.lower() + "-" + role
        tag = repository + ":" + revision
        try:
            engine.run("docker", "pull", "--platform", "linux/amd64", tag, timeout=1200, log=log)
        except UpdateError:
            if engine.image_mode == "pull":
                raise UpdateError("该提交的预构建镜像不可用；未停止当前服务。") from None
            return None  # auto falls back to the exact checked-out source.
        try:
            rows = json.loads(engine.run("docker", "image", "inspect", tag))
            image = rows[0]
            labels = image.get("Config", {}).get("Labels") or {}
            if (len(rows) != 1 or image.get("Os") != "linux" or image.get("Architecture") != "amd64"
                    or labels.get("org.opencontainers.image.revision") != revision
                    or labels.get("org.opencontainers.image.source", "").casefold() != engine.remote.removesuffix(".git").casefold()):
                raise ValueError
            digests = [value for value in image.get("RepoDigests", [])
                       if re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", value)]
            if len(set(digests)) != 1:
                raise ValueError
            result.append(digests[0])
        except (ValueError, KeyError, TypeError, IndexError):
            # Metadata mismatches are never silently downgraded to a source
            # build: a supposedly published artifact must identify itself.
            raise UpdateError("预构建镜像的来源、提交、平台或摘要不匹配，已取消更新。") from None
    return tuple(result)
