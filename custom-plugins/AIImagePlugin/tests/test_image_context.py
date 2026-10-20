"""Test source selection and bounded retention without SDK, network or QQ calls."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def context_env(monkeypatch):
    @dataclass(frozen=True)
    class GeneratedImage:
        base64: str | None = None
        url: str | None = None

    class ImageGenerationError(Exception):
        pass

    class Image:
        def __init__(self, base64="", url=""):
            self.base64, self.url = base64, url

    class Quote:
        def __init__(self, origin):
            self.origin = origin

    class EventListener:
        def __init__(self):
            self.handlers = {}

        def handler(self, event_type):
            def register(fn):
                self.handlers[event_type] = fn
                return fn

            return register

    class PersonMessageReceived:
        pass

    class GroupMessageReceived:
        pass

    class PromptPreProcessing:
        pass

    class QueryProxy:
        def __init__(self, *, query_id, query_uuid, plugin_runtime_handler):
            self.handler = plugin_runtime_handler
            self.handler.ref = (query_id, query_uuid)

        async def set_query_var(self, key, value):
            self.handler.written[key] = value

        async def get_bot_uuid(self):
            return "bot-a"

    modules = {
        "main": {
            "GeneratedImage": GeneratedImage,
            "ImageGenerationError": ImageGenerationError,
        },
        "langbot_plugin.api.entities.builtin.platform.message": {
            "Image": Image,
            "Quote": Quote,
        },
        "langbot_plugin.api.definition.components.common.event_listener": {
            "EventListener": EventListener
        },
        "langbot_plugin.api.entities": {
            "events": SimpleNamespace(
                PersonMessageReceived=PersonMessageReceived,
                GroupMessageReceived=GroupMessageReceived,
                PromptPreProcessing=PromptPreProcessing,
            )
        },
        "langbot_plugin.api.proxies.query_based_api": {
            "QueryBasedAPIProxy": QueryProxy
        },
    }
    for name, attrs in modules.items():
        stub = types.ModuleType(name)
        stub.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, stub)

    spec = importlib.util.spec_from_file_location(
        "image_context_under_test", ROOT / "pkg/image_context.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, "pkg.image_context", module)
    listener_spec = importlib.util.spec_from_file_location(
        "image_listener_under_test",
        ROOT / "components/event_listener/image_context.py",
    )
    listener_module = importlib.util.module_from_spec(listener_spec)
    listener_spec.loader.exec_module(listener_module)
    binding = SimpleNamespace(
        instance_uuid="instance-a",
        workspace_uuid="workspace-a",
        installation_uuid="installation-a",
        runtime_revision=1,
    )
    handler = SimpleNamespace(written={})
    plugin = SimpleNamespace(
        get_installation_binding=lambda: binding, plugin_runtime_handler=handler
    )
    now = [100.0]
    module._CACHE = module.ImageContextCache(clock=lambda: now[0])
    return SimpleNamespace(
        module=module,
        listener_module=listener_module,
        plugin=plugin,
        binding=binding,
        Image=Image,
        Quote=Quote,
        GeneratedImage=GeneratedImage,
        error=ImageGenerationError,
        handler=handler,
        now=now,
        event_type=PersonMessageReceived,
        group_event_type=GroupMessageReceived,
        prompt_event_type=PromptPreProcessing,
    )


def query(*images, **overrides):
    values = dict(
        bot_uuid="bot-a",
        launcher_type=SimpleNamespace(value="group"),
        launcher_id="chat-a",
        sender_id="sender-a",
        message_chain=list(images),
        instance_uuid="instance-a",
        workspace_uuid="workspace-a",
        placement_generation=1,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def variables(env, q):
    return {env.module.QUERY_CONTEXT_KEY: env.module.capture_context(env.plugin, q)}


def photo(env, label="first"):
    return env.Image(base64=f"data:image/png;base64,{label}")


def test_current_and_quoted_images_are_selected_without_model_urls(context_env):
    env = context_env
    current = variables(env, query(photo(env)))
    assert env.module.select_image(env.plugin, current).base64.endswith("first")
    quoted = variables(env, query(env.Quote([photo(env, "quoted")])))
    assert env.module.select_image(env.plugin, quoted).base64.endswith("quoted")
    encoded = json.dumps(quoted)
    assert (
        "base64" not in encoded and "data:" not in encoded and "https://" not in encoded
    )
    assert "sender-a" not in encoded and "chat-a" not in encoded


def test_unresolved_quote_does_not_reuse_older_image(context_env):
    env = context_env
    variables(env, query(photo(env, "older")))
    current = variables(env, query(env.Quote([])))
    with pytest.raises(env.error, match="重新引用"):
        env.module.select_image(env.plugin, current)
    # A following request must not revive the old target either.
    with pytest.raises(env.error, match="发送或引用"):
        env.module.select_image(env.plugin, variables(env, query()))


def test_unique_direct_image_takes_priority_over_unresolved_quote(context_env):
    env = context_env
    variables(env, query(photo(env, "older")))
    current = variables(env, query(env.Quote([]), photo(env, "current")))
    assert env.module.select_image(env.plugin, current).base64.endswith("current")


def test_resolved_and_unresolved_quotes_require_explicit_source(context_env):
    env = context_env
    current = variables(env, query(env.Quote([photo(env)]), env.Quote([])))
    with pytest.raises(env.error, match="重新引用"):
        env.module.select_image(env.plugin, current)


def test_recent_user_upload_can_be_edited_in_a_later_message(context_env):
    env = context_env
    variables(env, query(photo(env, "uploaded")))
    later = variables(env, query())
    assert later[env.module.QUERY_CONTEXT_KEY]["status"] == "recent"
    assert env.module.select_image(env.plugin, later).base64.endswith("uploaded")


@pytest.mark.asyncio
async def test_latest_user_or_successful_plugin_output_wins(context_env):
    env = context_env
    first = variables(env, query(photo(env, "upload-one")))
    output = env.GeneratedImage(url="https://images.example/output.png")
    api = SimpleNamespace(get_query_vars=AsyncMock(return_value=first))
    env.now[0] += 1
    await env.module.remember_output(env.plugin, api, output)
    later = variables(env, query())
    assert env.module.select_image(env.plugin, later) == output
    env.now[0] += 1
    current = variables(env, query(photo(env, "upload-two")))
    assert env.module.select_image(env.plugin, current).base64.endswith("upload-two")
    assert env.module.select_image(env.plugin, variables(env, query())).base64.endswith(
        "upload-two"
    )
    assert len(env.module._CACHE._entries) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"bot_uuid": "bot-b"},
        {"launcher_id": "chat-b"},
        {"sender_id": "sender-b"},
        {"launcher_type": SimpleNamespace(value="person")},
        {"placement_generation": 2},
    ],
)
def test_scope_prevents_other_bot_chat_or_sender_from_reusing_an_image(
    context_env, change
):
    env = context_env
    variables(env, query(photo(env)))
    with pytest.raises(env.error, match="没有可用"):
        env.module.select_image(env.plugin, variables(env, query(**change)))


def test_installations_and_workspaces_are_separate(context_env):
    env = context_env
    old = variables(env, query(photo(env)))
    other_binding = SimpleNamespace(
        instance_uuid="instance-a",
        workspace_uuid="workspace-b",
        installation_uuid="installation-b",
        runtime_revision=1,
    )
    other_plugin = SimpleNamespace(get_installation_binding=lambda: other_binding)
    fresh = {
        env.module.QUERY_CONTEXT_KEY: env.module.capture_context(
            other_plugin,
            query(workspace_uuid="workspace-b"),
        )
    }
    with pytest.raises(env.error):
        env.module.select_image(other_plugin, fresh)
    with pytest.raises(env.error):
        env.module.select_image(other_plugin, old)
    with pytest.raises(ValueError, match="scope"):
        env.module.capture_context(other_plugin, query())


def test_dedicated_plugin_instances_do_not_share_images(context_env):
    env = context_env
    first_plugin = SimpleNamespace()
    second_plugin = SimpleNamespace()
    first = env.module.capture_context(first_plugin, query(photo(env)))
    second = env.module.capture_context(second_plugin, query())
    assert first["owner"] != second["owner"]
    assert second["status"] == "missing"


def test_ambiguous_message_does_not_fall_back_to_a_previous_image(context_env):
    env = context_env
    variables(env, query(photo(env, "old")))
    ambiguous = variables(env, query(photo(env, "one"), env.Quote([photo(env, "two")])))
    with pytest.raises(env.error, match="多张"):
        env.module.select_image(env.plugin, ambiguous)
    with pytest.raises(env.error, match="没有可用"):
        env.module.select_image(env.plugin, variables(env, query()))


def test_expired_reference_does_not_rebind_to_a_newer_image(context_env):
    env = context_env
    old = variables(env, query(photo(env, "old")))
    variables(env, query(photo(env, "new")))
    with pytest.raises(env.error, match="过期"):
        env.module.select_image(env.plugin, old)
    latest = variables(env, query())
    env.now[0] += env.module.CACHE_TTL_SECONDS
    with pytest.raises(env.error, match="过期"):
        env.module.select_image(env.plugin, latest)
    assert len(env.module._CACHE._entries) == 0
    assert env.module._CACHE._bytes == 0


def test_cache_count_and_byte_limits_evict_oldest_entries(context_env):
    env = context_env
    cache = env.module.ImageContextCache(
        clock=lambda: env.now[0], max_entries=2, max_bytes=70
    )
    first = cache.put(
        "owner", "one", env.GeneratedImage(url="https://a.example/one.png")
    )
    second = cache.put(
        "owner", "two", env.GeneratedImage(url="https://a.example/two.png")
    )
    third = cache.put(
        "owner", "three", env.GeneratedImage(url="https://a.example/three.png")
    )
    assert cache.get("owner", "one", first) is None
    assert cache.get("owner", "two", second) is not None
    assert cache.get("owner", "three", third) is not None
    assert len(cache._entries) <= 2 and cache._bytes <= 70
    too_big = env.GeneratedImage(base64="data:image/png;base64," + "a" * 80)
    assert cache.put("owner", "large", too_big) is None
    assert cache.latest("owner", "two") == second


def test_foreign_scope_or_token_cannot_read_cached_bytes(context_env):
    env = context_env
    descriptor = variables(env, query(photo(env)))[env.module.QUERY_CONTEXT_KEY]
    assert (
        env.module._CACHE.get("other-owner", descriptor["scope"], descriptor["token"])
        is None
    )
    assert (
        env.module._CACHE.get(descriptor["owner"], "other-scope", descriptor["token"])
        is None
    )
    assert (
        env.module._CACHE.get(descriptor["owner"], descriptor["scope"], "invented")
        is None
    )


def test_revocation_removes_only_that_installation(context_env):
    env = context_env
    original = variables(env, query(photo(env)))
    other_owner = "unrelated-installation"
    other_token = env.module._CACHE.put(
        other_owner, "other-scope", env.GeneratedImage(url="https://a.example/a.png")
    )
    env.module.revoke_installation(env.binding)
    with pytest.raises(env.error, match="过期"):
        env.module.select_image(env.plugin, original)
    assert env.module._CACHE.get(other_owner, "other-scope", other_token) is not None


@pytest.mark.parametrize(
    "image",
    [
        {"base64": "not-a-data-uri"},
        {"url": "file:///private/image.png"},
        {"url": "https://user:secret@images.example/image.png"},
        {},
    ],
)
def test_invalid_source_is_not_cached_and_does_not_use_a_previous_image(
    context_env, image
):
    env = context_env
    variables(env, query(photo(env, "old")))
    invalid = variables(env, query(env.Image(**image)))
    with pytest.raises(env.error, match="没有可用"):
        env.module.select_image(env.plugin, invalid)
    with pytest.raises(env.error, match="没有可用"):
        env.module.select_image(env.plugin, variables(env, query()))


@pytest.mark.asyncio
async def test_listener_registers_message_events_and_writes_only_opaque_reference(
    context_env,
):
    env = context_env
    listener = env.listener_module.ImageContextListener()
    listener.plugin = env.plugin
    source = query(photo(env))
    event_ctx = SimpleNamespace(
        query_id=7,
        query_uuid="query-seven",
        event=source,
        instance_uuid=source.instance_uuid,
        workspace_uuid=source.workspace_uuid,
        placement_generation=source.placement_generation,
    )
    assert set(listener.handlers) == {
        env.event_type,
        env.group_event_type,
        env.prompt_event_type,
    }
    await listener.handlers[env.event_type](event_ctx)
    assert env.handler.ref == (7, "query-seven")
    descriptor = env.handler.written[env.module.QUERY_CONTEXT_KEY]
    assert descriptor["status"] == "current"
    assert set(descriptor) == {"owner", "scope", "status", "token"}
    assert "base64" not in json.dumps(descriptor)


@pytest.mark.asyncio
async def test_listener_missing_scope_is_safe_and_does_not_cache(context_env):
    env = context_env
    listener = env.listener_module.ImageContextListener()
    listener.plugin = env.plugin
    event_ctx = SimpleNamespace(
        query_id=7,
        query_uuid="query-seven",
        event=query(photo(env), sender_id=None),
    )
    await listener.capture(event_ctx)
    assert env.handler.written[env.module.QUERY_CONTEXT_KEY] == {
        "status": "unavailable"
    }
    assert len(env.module._CACHE._entries) == 0
