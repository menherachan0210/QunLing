from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from qunling.proactive.config import ConfigError, ConfigStore, DEFAULT_GROUP, defaults, validate


def config(mode="natural", **extra):
    return {**defaults(), "enabled": True, "groups": {"123456": {**deepcopy(DEFAULT_GROUP), "mode": mode}}, **extra}


def test_defaults_do_not_authorize_or_create_storage(tmp_path):
    store = ConfigStore("bot-a", tmp_path / "new")
    assert store.read() == defaults()
    assert store.read_valid and not store.root.exists()


def test_editable_default_survives_restart_legacy_client_and_reset(tmp_path):
    store = ConfigStore("a", tmp_path)
    old = config()
    assert validate(old) == old  # Existing files load without a migration write.
    stored = store.save({**old, "default_style": "  沉稳严谨，少说客套话。  "}, {"123456"})
    assert ConfigStore("a", tmp_path).read()["default_style"] == "沉稳严谨，少说客套话。"
    assert ConfigStore("b", tmp_path).read().get("default_style", "") == ""
    old_console = {key: value for key, value in stored.items() if key != "default_style"}
    saved = store.save(old_console, {"123456"})
    assert saved["default_style"] == stored["default_style"]
    reset = store.save({**saved, "default_style": ""}, {"123456"})
    assert reset["default_style"] == "" and reset["groups"] == stored["groups"]
    with pytest.raises(ConfigError, match="其他页面"):
        store.save({**saved, "default_style": "不应覆盖"}, {"123456"})


@pytest.mark.parametrize("style", [None, [], 42, "a" * 6001, "\ud800", "bad\x00style"])
def test_invalid_default_style_is_rejected(style):
    with pytest.raises(ConfigError):
        validate({**config(), "default_style": style})


def test_save_reload_is_bot_scoped_and_revision_checked(tmp_path):
    a, b = ConfigStore("a", tmp_path), ConfigStore("b", tmp_path)
    stored = a.save(config(), {"123456"})
    assert stored["revision"] != "0"
    assert ConfigStore("a", tmp_path).read() == stored
    assert b.read() == defaults()
    with pytest.raises(ConfigError, match="其他页面"):
        a.save(config(), {"123456"})
    assert a.read() == stored


def test_concurrent_writes_cannot_overwrite_each_other(tmp_path):
    def save(_):
        try:
            ConfigStore("a", tmp_path).save(config(), {"123456"})
            return "saved"
        except ConfigError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as workers:
        assert sorted(workers.map(save, range(2))) == ["revision_conflict", "saved"]


def test_unknown_and_departed_groups_cannot_be_enabled(tmp_path):
    store = ConfigStore("a", tmp_path)
    for mode in ("off", "quiet", "natural", "active"):
        with pytest.raises(ConfigError):
            store.save(config(mode), set())
    saved = store.save(config(), {"123456"})
    saved["groups"]["123456"]["mode"] = "off"
    store.save(saved, set())
    assert store.read()["groups"]["123456"]["mode"] == "off"


def test_offline_can_pause_but_never_grant_active_policy(tmp_path):
    store = ConfigStore("a", tmp_path)
    saved = store.save(config(), {"123456"})
    with pytest.raises(ConfigError):
        store.save(saved, None)
    saved["enabled"] = False
    paused = store.save(saved, None)
    paused["groups"]["777777"] = deepcopy(DEFAULT_GROUP)
    paused["groups"]["777777"]["mode"] = "active"
    with pytest.raises(ConfigError):
        store.save(paused, None)


def test_departed_group_does_not_prevent_global_pause(tmp_path):
    store = ConfigStore("a", tmp_path)
    saved = store.save(config(), {"123456"})
    saved["enabled"] = False
    saved = store.save(saved, set())
    assert not saved["enabled"]
    saved["enabled"] = True
    with pytest.raises(ConfigError):
        store.save(saved, set())


@pytest.mark.parametrize("value", [None, [], True, {"version": 1}, {**defaults(), "enabled": 1},
    {**defaults(), "revision": []}, {**defaults(), "groups": {"../bad": {}}}])
def test_invalid_top_level_is_rejected(value):
    with pytest.raises(ConfigError):
        validate(value)


@pytest.mark.parametrize("field,value", [("mode", []), ("mode", "random"),
    ("persona", "a" * 1001), ("persona", "\ud800"), ("display_name", ""),
    ("display_name", "x\nname"), ("quiet_hours", {}), ("quiet_hours", None)])
def test_invalid_group_policy_is_rejected(field, value):
    data = config()
    data["groups"]["123456"][field] = value
    with pytest.raises(ConfigError):
        validate(data)


@pytest.mark.parametrize("changes", [{"timezone": "../bad"}, {"timezone": "No/Such_Zone"},
    {"start": "24:01"}, {"end": None}, {"enabled": 1}, {"enabled": True, "start": "08:00"}])
def test_invalid_quiet_hours(changes):
    data = config()
    data["groups"]["123456"]["quiet_hours"].update(changes)
    with pytest.raises(ConfigError):
        validate(data)


@pytest.mark.parametrize("corrupt", ["not-json", '{"version":1}', '[]', '"secret"', "x" * 270000],
                         ids=["json", "schema", "array", "scalar", "oversize"])
def test_corrupt_configuration_fails_closed(tmp_path, corrupt):
    store = ConfigStore("a", tmp_path)
    store.path.write_text(corrupt)
    assert store.read() == defaults() and not store.read_valid


def test_symlink_never_exposes_or_overwrites_target(tmp_path):
    store = ConfigStore("a", tmp_path)
    target = tmp_path / "other.json"
    target.write_text(json.dumps(config()))
    try:
        store.path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    assert store.read() == defaults() and not store.read_valid
    # A corrupted/symlink path must not be considered an editable valid state.
    with pytest.raises(ConfigError):
        store.save(config(), {"123456"})
    assert json.loads(target.read_text()) == config()
