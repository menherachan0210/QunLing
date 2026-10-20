"""Verify bounded, scoped TXT references without reading files or using a network."""

from __future__ import annotations

import base64
import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from langbot_plugin.api.entities.builtin.platform.message import (
    File,
    MessageChain,
    Plain,
    Quote,
)
from langbot_plugin.entities.io.context import InstallationBinding


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(monkeypatch):
    for name, filename in (
        ("pkg.reader", "reader.py"),
        ("pkg.text_context", "text_context.py"),
    ):
        spec = importlib.util.spec_from_file_location(name, ROOT / "pkg" / filename)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    binding = InstallationBinding(
        instance_uuid="instance-a",
        workspace_uuid="workspace-a",
        installation_uuid="installation-a",
        runtime_revision=1,
        placement_generation=1,
        artifact_digest="a" * 64,
    )
    plugin = SimpleNamespace(get_installation_binding=lambda: binding)
    return SimpleNamespace(module=module, binding=binding, plugin=plugin)


def source(chain=(), **changes):
    values = dict(
        bot_uuid="bot-a",
        launcher_type="group",
        launcher_id="chat-a",
        sender_id="sender-a",
        instance_uuid="instance-a",
        workspace_uuid="workspace-a",
        placement_generation=1,
        message_chain=MessageChain(list(chain)),
    )
    values.update(changes)
    return SimpleNamespace(**values)


def file(file_id="file-a", **changes):
    values = dict(id=file_id, name="fixture.txt", size=11)
    values.update(changes)
    return File(**values)


def capture(env, chain=(), **changes):
    descriptor = env.module.capture_context(env.plugin, source(chain, **changes))
    return {env.module.QUERY_CONTEXT_KEY: descriptor}


def attachment(module, file_id="file-a", **changes):
    values = dict(
        bot_uuid="bot-a",
        chat_type="group",
        chat_id="chat-a",
        sender_id="sender-a",
        file_id=file_id,
        name="fixture.txt",
        size=11,
    )
    values.update(changes)
    return module.Attachment(**values)


def test_default_cache_limits_and_count_eviction(env):
    module = env.module
    cache = module.AttachmentCache()
    assert (cache.max_entries, cache.max_bytes, cache.ttl) == (
        64,
        8 * 1024 * 1024,
        1800,
    )
    tokens = [
        cache.put("owner", "scope", attachment(module, f"file-{i}")) for i in range(65)
    ]
    assert len(cache.entries) == 64
    assert cache.get("owner", "scope", tokens[0]) is None
    assert cache.get("owner", "scope", tokens[-1]).file_id == "file-64"
    assert cache.latest("owner", "scope") == tokens[-1]


def test_default_byte_budget_evicts_before_count_limit(env):
    module = env.module
    cache = module.AttachmentCache()
    inline = base64.b64encode(b"x" * module.MAX_BYTES).decode()
    tokens = [
        cache.put("owner", "scope", attachment(module, f"file-{i}", inline=inline))
        for i in range(64)
    ]
    assert 0 < len(cache.entries) < 64
    assert cache.bytes <= 8 * 1024 * 1024
    assert cache.bytes == sum(entry.cost for entry in cache.entries.values())
    assert cache.get("owner", "scope", tokens[0]) is None
    assert cache.latest("owner", "scope") == tokens[-1]


def test_single_over_budget_attachment_cannot_evict_valid_entries(env):
    cache = env.module.AttachmentCache(max_bytes=200)
    token = cache.put("owner", "scope", attachment(env.module))
    assert token
    assert (
        cache.put("owner", "scope", attachment(env.module, "other", inline="x" * 201))
        is None
    )
    assert cache.latest("owner", "scope") == token


def test_duplicate_merges_payload_without_extending_ttl_or_reordering_latest(env):
    now = [0.0]
    cache = env.module.AttachmentCache(clock=lambda: now[0], ttl=30)
    original = attachment(env.module)
    old_token = cache.put("owner", "scope", original)
    now[0] = 5
    latest = cache.put("owner", "scope", attachment(env.module, "file-b"))
    now[0] = 20
    duplicate = replace(original, url="https://example.invalid/file")
    assert cache.put("owner", "scope", duplicate) == old_token
    assert cache.entries[old_token].created == 0
    assert cache.latest("owner", "scope") == latest
    assert cache.get("owner", "scope", old_token).url == duplicate.url
    now[0] = 30
    assert cache.get("owner", "scope", old_token) is None
    assert cache.latest("owner", "scope") == latest
    now[0] = 35
    assert cache.latest("owner", "scope") is None
    assert cache.bytes == 0


def test_duplicate_growth_cannot_exceed_byte_budget(env):
    cache = env.module.AttachmentCache(max_bytes=150)
    original = attachment(env.module)
    token = cache.put("owner", "scope", original)
    cache.put("owner", "other-scope", attachment(env.module, "other"))
    assert cache.put("owner", "scope", replace(original, inline="x" * 100)) == token
    assert cache.get("owner", "scope", token) == original
    assert cache.bytes <= 150


@pytest.mark.parametrize(
    "field,value",
    [
        ("bot_uuid", "bot-b"),
        ("launcher_type", "person"),
        ("launcher_id", "chat-b"),
        ("sender_id", "sender-b"),
    ],
)
def test_recent_lookup_isolated_by_platform_scope(env, field, value):
    current = capture(env, [file()])
    different = capture(env, **{field: value})
    assert different[env.module.QUERY_CONTEXT_KEY]["status"] == "missing"
    assert env.module.select_attachment(env.plugin, current).file_id == "file-a"


@pytest.mark.parametrize(
    "field,value",
    [
        ("instance_uuid", "instance-b"),
        ("workspace_uuid", "workspace-b"),
        ("installation_uuid", "installation-b"),
        ("runtime_revision", 2),
        ("placement_generation", 2),
    ],
)
def test_attachment_tokens_are_isolated_by_installation_binding(env, field, value):
    variables = capture(env, [file()])
    other_binding = env.binding.model_copy(update={field: value})
    other_plugin = SimpleNamespace(get_installation_binding=lambda: other_binding)
    with pytest.raises(env.module.TextReadError, match="没有"):
        env.module.select_attachment(other_plugin, variables)


@pytest.mark.parametrize(
    "field,value",
    [
        ("instance_uuid", "wrong-instance"),
        ("workspace_uuid", "wrong-workspace"),
        ("placement_generation", 2),
    ],
)
def test_inconsistent_source_and_installation_scope_is_rejected(env, field, value):
    with pytest.raises(ValueError, match="scope"):
        capture(env, [file()], **{field: value})


def test_dedicated_plugin_instances_cannot_share_tokens(env):
    left, right = SimpleNamespace(), SimpleNamespace()
    descriptor = env.module.capture_context(left, source([file()]))
    with pytest.raises(env.module.TextReadError):
        env.module.select_attachment(right, {env.module.QUERY_CONTEXT_KEY: descriptor})


def test_revoking_one_installation_preserves_another(env):
    first = capture(env, [file()])
    second_binding = env.binding.model_copy(
        update={"installation_uuid": "installation-b"}
    )
    second_plugin = SimpleNamespace(get_installation_binding=lambda: second_binding)
    second = env.module.capture_context(second_plugin, source([file("file-b")]))
    env.module.revoke_installation(env.binding)
    with pytest.raises(env.module.TextReadError, match="失效"):
        env.module.select_attachment(env.plugin, first)
    assert (
        env.module.select_attachment(
            second_plugin, {env.module.QUERY_CONTEXT_KEY: second}
        ).file_id
        == "file-b"
    )


@pytest.mark.parametrize(
    "chain,status",
    [
        ([Quote(origin=MessageChain([]))], "unresolved_quote"),
        ([Quote(origin=MessageChain([Plain(text="not a file")]))], "unresolved_quote"),
        ([file("one"), file("two")], "ambiguous"),
        ([file(name="unsupported.pdf")], "unsupported"),
    ],
)
def test_ambiguous_or_unreadable_current_target_clears_recent_fallback(
    env, chain, status
):
    capture(env, [file()])
    variables = capture(env, chain)
    assert variables[env.module.QUERY_CONTEXT_KEY]["status"] == status
    with pytest.raises(env.module.TextReadError):
        env.module.select_attachment(env.plugin, variables)
    assert capture(env)[env.module.QUERY_CONTEXT_KEY]["status"] == "missing"


def test_current_file_takes_precedence_over_recent_and_quoted_files(env):
    old = capture(env, [file("old")])
    quoted = Quote(origin=MessageChain([file("quoted-a"), file("quoted-b")]))
    current = capture(env, [quoted, file("current")])
    assert env.module.select_attachment(env.plugin, current).file_id == "current"
    assert env.module.select_attachment(env.plugin, old).file_id == "old"


def test_cross_group_quote_cannot_select_a_file_or_fall_back_to_recent(env):
    capture(env, [file("old")], launcher_id="111")
    quote = Quote(group_id=222, origin=MessageChain([file("foreign")]))
    variables = capture(env, [quote], launcher_id="111")
    assert variables[env.module.QUERY_CONTEXT_KEY]["status"] == "unresolved_quote"
    assert (
        capture(env, launcher_id="111")[env.module.QUERY_CONTEXT_KEY]["status"]
        == "missing"
    )


def test_cross_person_quote_cannot_select_a_file_or_fall_back_to_recent(env):
    capture(env, [file("old")], launcher_type="person", launcher_id="111")
    quote = Quote(target_id=222, origin=MessageChain([file("foreign")]))
    variables = capture(env, [quote], launcher_type="person", launcher_id="111")
    assert variables[env.module.QUERY_CONTEXT_KEY]["status"] == "unresolved_quote"
    assert (
        capture(env, launcher_type="person", launcher_id="111")[
            env.module.QUERY_CONTEXT_KEY
        ]["status"]
        == "missing"
    )


def test_inline_utf8_is_preserved_without_entering_query_variables(env):
    raw = "示例正文，半角标点?\n".encode("utf-8")
    inline = "data:text/plain;base64," + base64.b64encode(raw).decode()
    variables = capture(env, [file(base64=inline, size=len(raw))])
    selected = env.module.select_attachment(env.plugin, variables)
    assert env.module.decode_inline(selected.inline) == raw
    assert inline not in str(variables)
    assert "fixture.txt" not in str(variables)


@pytest.mark.parametrize(
    "inline",
    [
        "not-valid-base64!",
        base64.b64encode(b"x" * (128 * 1024 + 1)).decode(),
    ],
    ids=["invalid-base64", "over-byte-limit"],
)
def test_invalid_or_oversized_inline_is_rejected_before_cache(env, inline):
    variables = capture(env, [file(base64=inline)])
    assert variables[env.module.QUERY_CONTEXT_KEY]["status"] == "unsupported"
    assert env.module._CACHE.bytes == 0


def test_new_notice_keeps_older_inflight_query_token_and_updates_latest(env):
    old = capture(env, [file("old", url="https://example.invalid/old")])
    env.module.capture_group_upload(
        env.plugin,
        "bot-a",
        "chat-a",
        "sender-a",
        {
            "id": "new",
            "name": "new.txt",
            "size": 17,
            "url": "https://example.invalid/ignored",
        },
        instance_uuid="instance-a",
        workspace_uuid="workspace-a",
        placement_generation=1,
    )
    latest = capture(env)
    assert env.module.select_attachment(env.plugin, old).file_id == "old"
    selected = env.module.select_attachment(env.plugin, latest)
    assert selected.file_id == "new"
    assert selected.url == ""


@pytest.mark.parametrize("notice_first", [False, True])
def test_native_file_and_notice_deduplicate_without_refreshing_ttl(
    env, notice_first, monkeypatch
):
    now = [0.0]
    monkeypatch.setattr(
        env.module, "_CACHE", env.module.AttachmentCache(clock=lambda: now[0], ttl=30)
    )

    def notice():
        env.module.capture_group_upload(
            env.plugin,
            "bot-a",
            "chat-a",
            "sender-a",
            {"id": "file-a", "name": "fixture.txt", "size": 11},
        )

    if notice_first:
        notice()
        original = capture(env)
    else:
        original = capture(env, [file(url="https://example.invalid/file")])
    now[0] = 20
    if notice_first:
        duplicate = capture(env, [file(url="https://example.invalid/file")])
    else:
        notice()
        duplicate = capture(env)
    assert (
        original[env.module.QUERY_CONTEXT_KEY]["token"]
        == duplicate[env.module.QUERY_CONTEXT_KEY]["token"]
    )
    assert len(env.module._CACHE.entries) == 1
    assert (
        env.module.select_attachment(env.plugin, original).url
        == "https://example.invalid/file"
    )
    now[0] = 30
    with pytest.raises(env.module.TextReadError, match="过期"):
        env.module.select_attachment(env.plugin, original)
