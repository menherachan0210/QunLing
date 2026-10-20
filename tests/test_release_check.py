import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "release_check", Path(__file__).resolve().parents[1] / "Check-Release.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.mark.parametrize(
    "name",
    [
        ".local/foo.json",
        "langbot/data/config.yaml",
        "backup.sqlite",
        "secrets.json",
        ".env.production",
    ],
)
def test_private_files_are_flagged_even_without_recognizable_credentials(name):
    assert release.inspect(name, b"synthetic data")


def test_detects_credential_without_returning_value():
    key = "sk-" + "X" * 40
    reasons = release.inspect("config.yaml", f"api_key: {key}".encode())
    assert reasons
    assert key not in str(reasons)


def test_source_and_distributable_environment_example_are_allowed():
    assert not release.inspect(
        "bootstrap/server.py", b'api_key = values.get("api_key")'
    )
    assert not release.inspect(".env.example", b"API_KEY=replace-with-your-own")
