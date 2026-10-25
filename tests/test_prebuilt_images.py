import json
from types import SimpleNamespace

import pytest

from deploy.updater.engine import UpdateError
from deploy.updater.images import pull_prebuilt

REVISION = "a" * 40
REPOSITORY = "Example/QunLing"


def engine(*, mode="auto", unavailable=False, mismatch=None):
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        if args[1] == "pull":
            if unavailable:
                raise UpdateError("unavailable")
            return ""
        repo = args[-1].rsplit(":", 1)[0]
        item = {"Os": "linux", "Architecture": "amd64", "RepoDigests": [repo + "@sha256:" + "b" * 64],
                "Config": {"Labels": {"org.opencontainers.image.revision": REVISION,
                                      "org.opencontainers.image.source": "https://github.com/" + REPOSITORY}}}
        if mismatch == "revision":
            item["Config"]["Labels"]["org.opencontainers.image.revision"] = "c" * 40
        elif mismatch == "source":
            item["Config"]["Labels"]["org.opencontainers.image.source"] = "https://github.com/other/repo"
        elif mismatch == "digest":
            item["RepoDigests"] = ["ghcr.io/other/repo@sha256:" + "b" * 64]
        elif mismatch == "platform":
            item["Architecture"] = "arm64"
        return json.dumps([item])
    return SimpleNamespace(repository=REPOSITORY, remote="https://github.com/"+REPOSITORY+".git",
                           image_mode=mode, run=run, calls=calls)


def test_published_pair_is_pinned_to_digest_and_exact_commit():
    candidate = engine()
    images = pull_prebuilt(candidate, REVISION, None)
    assert len(images) == 2 and all("@sha256:" in value for value in images)
    assert [call[-1] for call in candidate.calls if call[1] == "pull"] == [
        "ghcr.io/example/qunling-app:"+REVISION, "ghcr.io/example/qunling-updater:"+REVISION]


@pytest.mark.parametrize("mismatch", ["revision", "source", "digest", "platform"])
def test_invalid_artifacts_never_fall_back_silently(mismatch):
    with pytest.raises(UpdateError):
        pull_prebuilt(engine(mismatch=mismatch), REVISION, None)


def test_unpublished_commit_falls_back_only_in_auto_mode():
    assert pull_prebuilt(engine(unavailable=True), REVISION, None) is None
    with pytest.raises(UpdateError):
        pull_prebuilt(engine(mode="pull", unavailable=True), REVISION, None)
