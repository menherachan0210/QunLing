"""Private setup state tests; every file and credential here is synthetic."""

import json
import os

import pytest

from bootstrap import state


def test_prepare_creates_matching_private_tokens_and_safe_empty_connections(tmp_path):
    private, napcat = tmp_path / "private", tmp_path / "napcat"
    values = state.prepare(private, napcat)
    assert set(values) == {"setup_token", "napcat_token", "onebot_token"}
    assert len(set(values.values())) == 3
    assert all(len(value) >= 32 for value in values.values())
    assert state.read_json(private / "secrets.json") == values
    webui = state.read_json(napcat / "webui.json")
    assert webui["token"] == values["napcat_token"]
    assert webui["port"] == 6099
    assert webui["disableWebUI"] is False
    config = state.read_json(napcat / "onebot11.json")
    assert all(not items for items in config["network"].values())
    assert config["enableLocalFile2Url"] is False
    if os.name != "nt":
        assert (private / "secrets.json").stat().st_mode & 0o777 == 0o600


def test_prepare_rerun_preserves_tokens_existing_connections_and_webui_options(tmp_path):
    private, napcat = tmp_path / "private", tmp_path / "napcat"
    first = state.prepare(private, napcat)
    existing = {"network": {"websocketClients": [{"name": "unrelated"}]}}
    state.write_json(napcat / "onebot11.json", existing)
    webui = state.read_json(napcat / "webui.json")
    webui["port"] = 6100
    webui["custom"] = "preserved"
    state.write_json(napcat / "webui.json", webui)
    assert state.prepare(private, napcat) == first
    assert state.read_json(napcat / "onebot11.json") == existing
    assert state.read_json(napcat / "webui.json") == webui


def test_prepare_adopts_existing_webui_token_without_replacing_options(tmp_path):
    private, napcat = tmp_path / "private", tmp_path / "napcat"
    webui = {"token": "synthetic-existing-token", "port": 6101, "custom": True}
    state.write_json(napcat / "webui.json", webui)
    result = state.prepare(private, napcat)
    assert result["napcat_token"] == webui["token"]
    assert state.read_json(napcat / "webui.json") == webui


def test_prepare_fills_missing_token_in_nonempty_webui(tmp_path):
    private, napcat = tmp_path / "private", tmp_path / "napcat"
    state.write_json(napcat / "webui.json", {"port": 6101, "custom": True})
    result = state.prepare(private, napcat)
    webui = state.read_json(napcat / "webui.json")
    assert webui["token"] == result["napcat_token"]
    assert webui["port"] == 6101 and webui["custom"] is True


def test_prepare_reconciles_changed_webui_token_without_rotating_other_tokens(tmp_path):
    private, napcat = tmp_path / "private", tmp_path / "napcat"
    first = state.prepare(private, napcat)
    webui = state.read_json(napcat / "webui.json")
    webui["token"] = "synthetic-manually-updated-token"
    state.write_json(napcat / "webui.json", webui)
    second = state.prepare(private, napcat)
    assert second["napcat_token"] == webui["token"]
    assert second["setup_token"] == first["setup_token"]
    assert second["onebot_token"] == first["onebot_token"]
    assert state.read_json(private / "secrets.json") == second


def test_atomic_write_failure_preserves_previous_file_and_cleans_temporary(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    state.write_json(path, {"previous": "value"})

    def fail_replace(*_args):
        raise OSError("synthetic failure")

    monkeypatch.setattr(state.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic failure"):
        state.write_json(path, {"new": "value"})
    assert json.loads(path.read_text()) == {"previous": "value"}
    assert list(tmp_path.glob("*.tmp")) == []


def test_state_round_trip_and_missing_default_are_isolated(tmp_path):
    private = state.State(tmp_path)
    assert private.read(default={"empty": True}) == {"empty": True}
    private.write({"name": "合成测试"})
    private.write({"user": "test@local.invalid"}, "langbot-admin.json")
    assert private.read() == {"name": "合成测试"}
    assert private.read("langbot-admin.json") == {"user": "test@local.invalid"}
