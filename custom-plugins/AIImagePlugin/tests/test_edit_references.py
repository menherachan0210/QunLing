"""Exercise real SDK reference selection with fake Host calls and synthetic images."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langbot_plugin.api.entities.builtin.platform.message import (
    Image,
    MessageChain,
    Plain,
    Quote,
    Source,
)

from main import ImageGenerationError


ROOT = Path(__file__).resolve().parents[1]
NOW = 1800000000


@pytest.fixture
def ref_env(monkeypatch):
    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, ROOT / path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    cache = load("image_cache_references_test", "pkg/image_context.py")
    monkeypatch.setitem(sys.modules, "pkg.image_context", cache)
    refs = load("editing_references_test", "pkg/edit_references.py")
    monkeypatch.setitem(sys.modules, "pkg.edit_references", refs)
    listing = load(
        "editing_list_test", "components/tools/list_edit_image_references.py"
    )
    clock = [100.0]
    refs.STORE = refs.ReferenceStore(clock=lambda: clock[0])
    binding = SimpleNamespace(
        instance_uuid="instance",
        workspace_uuid="workspace",
        installation_uuid="installation",
        runtime_revision=1,
    )
    plugin = SimpleNamespace(
        get_config=lambda: {},
        get_installation_binding=lambda: binding,
        plugin_runtime_handler=object(),
        call_platform_api=AsyncMock(return_value={"messages": []}),
    )
    return SimpleNamespace(
        refs=refs,
        plugin=plugin,
        binding=binding,
        clock=clock,
        cache=cache,
        listing=listing,
        api=SimpleNamespace(get_bot_uuid=AsyncMock(return_value="bot")),
    )


def query(*images, query_id=1, sender="101", chat="201", kind="group", timestamp=NOW):
    return SimpleNamespace(
        query_id=query_id,
        query_uuid=f"query-{query_id}",
        bot_uuid="bot",
        launcher_type=kind,
        launcher_id=chat,
        sender_id=sender,
        instance_uuid="instance",
        workspace_uuid="workspace",
        placement_generation=1,
        message_chain=MessageChain(
            [
                Source(
                    id=str(query_id),
                    time=datetime.fromtimestamp(timestamp, timezone.utc),
                ),
                *images,
            ]
        ),
    )


def picture(name):
    return Image(url=f"https://image.example/{name}.png?synthetic-token=secret")


def capture(env, source):
    return {env.refs.REFERENCE_KEY: env.refs.capture_references(env.plugin, source)}


async def listing(env, variables, query_id=1):
    return await env.refs.list_references(
        env.plugin,
        env.api,
        variables,
        query_id,
        f"query-{query_id}",
    )


def selected(env, variables, refs, query_id=1):
    return env.refs.select_references(
        env.plugin,
        variables,
        query_id,
        f"query-{query_id}",
        refs,
    )


def group_message(number, *, sender="102", images=None, timestamp=None, **extra):
    return {
        "message_id": str(number),
        "time": timestamp or NOW - number,
        "sender_id": sender,
        "sender_name": "同名",
        "text": f"reference {number}",
        "images": images or [{"url": picture(str(number)).url}],
        **extra,
    }


@pytest.mark.asyncio
async def test_natural_request_can_edit_a_later_image_from_its_frozen_batch(ref_env, tmp_path, monkeypatch):
    from qunling.group_batch import publish
    from qunling.integration import workspace_key
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    env = ref_env
    variables = capture(env, query(Plain(text="把这张改成水彩")))
    row = SimpleNamespace(bot_id="bot", group_id="201", sender_id="101", sender_name="成员",
        message_id="21", timestamp=NOW + 2, text="", reply_to=None, is_bot=False, has_media=True)
    assert publish(workspace_key(env.plugin), "bot", "201", "101", "1", [row], "999")
    env.plugin.call_platform_api.side_effect = [{"messages": []}, {
        "message_id": "21", "group_id": "201", "sender": {"user_id": "101"},
        "message": [{"type": "image", "data": {"url": picture("later").url}}],
    }]
    result = await listing(env, variables)
    assert len(result["images"]) == 1
    actual = selected(env, variables, [result["images"][0]["image_ref"]])
    assert actual[0].url == picture("later").url
    assert await listing(env, variables) == result
    assert env.plugin.call_platform_api.await_count == 2


@pytest.mark.asyncio
async def test_two_group_images_from_another_member_are_selectable_in_requested_order(
    ref_env,
):
    env = ref_env
    variables = capture(env, query(Plain(text="把前两张角色衣服互换")))
    env.plugin.call_platform_api.return_value = {
        "messages": [group_message(10), group_message(11)],
    }
    result = await listing(env, variables)
    assert len(result["images"]) == 2
    assert all(not row["is_questioner"] for row in result["images"])
    refs = [row["image_ref"] for row in result["images"]]
    images = selected(env, variables, refs)
    assert [image.url for image in images] == [picture("10").url, picture("11").url]
    env.plugin.call_platform_api.assert_awaited_once_with(
        "bot",
        "call_platform_api",
        {
            "action": "get_group_recent_context",
            "params": {
                "group_id": "201",
                "before_message_id": "1",
                "before_time": NOW,
                "window_seconds": 86400,
                "max_messages": 1000,
            },
        },
    )
    encoded = json.dumps(result)
    assert "https://" not in encoded and "synthetic-token" not in encoded
    assert "data:image" not in encoded
    assert "group_id" not in encoded and "sender_id" not in encoded
    assert all(ref.startswith("edit_ref_") for ref in refs)
    assert "img_" not in refs[0]


@pytest.mark.asyncio
async def test_list_is_a_frozen_snapshot_and_concurrent_calls_fetch_once(ref_env):
    env = ref_env
    variables = capture(env, query())
    env.plugin.call_platform_api.return_value = {"messages": [group_message(10)]}
    first, second = await asyncio.gather(
        listing(env, variables), listing(env, variables)
    )
    assert first == second
    env.plugin.call_platform_api.assert_awaited_once()
    env.plugin.call_platform_api.return_value = {"messages": [group_message(20)]}
    assert await listing(env, variables) == first


@pytest.mark.asyncio
async def test_same_message_and_same_second_keep_original_upload_order(ref_env):
    env = ref_env
    variables = capture(env, query())
    env.plugin.call_platform_api.return_value = {
        "messages": [
            group_message(
                10,
                timestamp=NOW - 1,
                images=[
                    {"index": 2, "url": picture("first").url},
                    {"index": 4, "url": picture("second").url},
                ],
            ),
            group_message(
                11,
                timestamp=NOW - 1,
                images=[
                    {"index": 0, "url": picture("third").url},
                ],
            ),
        ]
    }
    rows = (await listing(env, variables))["images"]
    assert [row["message_image_index"] for row in rows] == [1, 2, 1]
    assert [row["chronological_order"] for row in rows] == [1, 2, 3]
    assert rows[0]["message_ref"] == rows[1]["message_ref"] != rows[2]["message_ref"]
    assert [
        image.url
        for image in selected(env, variables, [row["image_ref"] for row in rows])
    ] == [
        picture("first").url,
        picture("second").url,
        picture("third").url,
    ]


@pytest.mark.asyncio
async def test_newest_candidate_window_keeps_its_chronological_order(
    ref_env, monkeypatch
):
    env = ref_env
    monkeypatch.setattr(env.refs, "MAX_CANDIDATES", 2)
    variables = capture(env, query())
    env.plugin.call_platform_api.return_value = {
        "messages": [
            group_message(10),
            group_message(11),
            group_message(12),
        ]
    }
    rows = (await listing(env, variables))["images"]
    assert [
        image.url
        for image in selected(env, variables, [row["image_ref"] for row in rows])
    ] == [
        picture("11").url,
        picture("12").url,
    ]


def test_current_multiphoto_prompt_exposes_selectable_refs_without_network(ref_env):
    env = ref_env
    variables = capture(env, query(picture("one"), picture("two")))
    context = env.refs.STORE.get(env.plugin, variables, 1, "query-1")
    hidden_refs = list(context.refs)
    with pytest.raises(ImageGenerationError, match="本轮"):
        selected(env, variables, hidden_refs)
    current = env.refs.current_references(env.plugin, variables, 1, "query-1")
    refs = [row["image_ref"] for row in current]
    assert len(refs) == 2 and all(row["source"] == "current" for row in current)
    assert [image.url for image in selected(env, variables, refs)] == [
        picture("one").url,
        picture("two").url,
    ]
    env.plugin.call_platform_api.assert_not_awaited()


def test_quoted_other_member_image_can_be_selected(ref_env):
    env = ref_env
    variables = capture(env, query(Quote(origin=MessageChain([picture("quoted")]))))
    rows = env.refs.current_references(env.plugin, variables, 1, "query-1")
    assert rows[0]["source"] == "quoted"
    assert (
        selected(env, variables, [rows[0]["image_ref"]])[0].url == picture("quoted").url
    )


@pytest.mark.asyncio
async def test_unresolved_quote_is_not_disguised_as_recent_image(ref_env):
    env = ref_env
    capture(env, query(picture("previous"), kind="person"))
    variables = capture(
        env, query(Quote(origin=MessageChain([])), query_id=2, kind="person")
    )
    result = await listing(env, variables, 2)
    assert len(result["images"]) == 1
    assert result["images"][0]["source"] == "recent"
    assert result["unavailable_images"][0]["source"] == "quoted"
    assert "不能" in result["unavailable_images"][0]["reason"]


@pytest.mark.asyncio
async def test_explicit_reference_requires_matching_host_bot(ref_env):
    env = ref_env
    variables = capture(env, query(picture("one"), kind="person"))
    refs = [row["image_ref"] for row in (await listing(env, variables))["images"]]
    with pytest.raises(ImageGenerationError, match="不属于"):
        env.refs.select_references(
            env.plugin, variables, 1, "query-1", refs, bot="another-bot"
        )


@pytest.mark.asyncio
async def test_successfully_delivered_output_is_available_only_to_same_sender_next_query(
    ref_env,
):
    env = ref_env
    variables = capture(env, query(kind="person"))
    generated = env.refs.GeneratedImage(url=picture("generated").url)
    env.refs.remember_delivered(env.plugin, variables, generated)
    newer = capture(env, query(query_id=2, kind="person"))
    rows = (await listing(env, newer, 2))["images"]
    assert len(rows) == 1
    assert selected(env, newer, [rows[0]["image_ref"]], 2)[0].url == generated.url
    different = capture(env, query(query_id=3, kind="person", sender="103"))
    assert (await listing(env, different, 3))["images"] == []


@pytest.mark.asyncio
async def test_private_consecutive_uploads_remain_selectable_without_group_history(
    ref_env,
):
    env = ref_env
    capture(env, query(picture("first"), query_id=1, kind="person", timestamp=NOW - 2))
    capture(env, query(picture("second"), query_id=2, kind="person", timestamp=NOW - 1))
    variables = capture(env, query(query_id=3, kind="person"))
    result = await listing(env, variables, 3)
    assert len(result["images"]) == 2
    assert all(row["source"] == "recent" for row in result["images"])
    assert (
        len(selected(env, variables, [row["image_ref"] for row in result["images"]], 3))
        == 2
    )
    env.plugin.call_platform_api.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [{"sender": "102"}, {"chat": "202"}, {"kind": "group"}]
)
async def test_recent_uploads_do_not_cross_sender_conversation_or_chat_type(
    ref_env, change
):
    env = ref_env
    capture(env, query(picture("private"), kind="person"))
    values = {"kind": "person", **change}
    variables = capture(env, query(query_id=2, **values))
    result = await listing(env, variables, 2)
    assert result["images"] == []


@pytest.mark.asyncio
async def test_new_query_cannot_reuse_old_ref_even_for_same_person_and_image(ref_env):
    env = ref_env
    variables = capture(env, query(picture("same"), kind="person"))
    old = (await listing(env, variables))["images"][0]["image_ref"]
    newer = capture(env, query(query_id=2, kind="person"))
    current = (await listing(env, newer, 2))["images"][0]["image_ref"]
    assert old != current
    with pytest.raises(ImageGenerationError):
        selected(env, newer, [old], 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refs", [[], ["img_1"], ["https://image.example/a"], [None], ["edit_ref_madeup"]]
)
async def test_invalid_or_foreign_tool_refs_fail_closed(ref_env, refs):
    env = ref_env
    variables = capture(env, query(picture("one"), kind="person"))
    await listing(env, variables)
    with pytest.raises(ImageGenerationError):
        selected(env, variables, refs)


@pytest.mark.asyncio
async def test_duplicate_and_more_than_four_refs_are_rejected(ref_env):
    env = ref_env
    variables = capture(env, query(*(picture(str(i)) for i in range(5)), kind="person"))
    refs = [row["image_ref"] for row in (await listing(env, variables))["images"]]
    for bad in [refs[:1] * 2, refs]:
        with pytest.raises(ImageGenerationError):
            selected(env, variables, bad)
    assert len(selected(env, variables, refs[:4])) == 4


@pytest.mark.asyncio
async def test_expired_query_and_revoked_installation_invalidate_refs(ref_env):
    env = ref_env
    variables = capture(env, query(picture("one"), kind="person"))
    refs = [row["image_ref"] for row in (await listing(env, variables))["images"]]
    env.clock[0] += env.refs.QUERY_TTL
    with pytest.raises(ImageGenerationError, match="过期"):
        selected(env, variables, refs)
    variables = capture(env, query(picture("two"), kind="person"))
    refs = [row["image_ref"] for row in (await listing(env, variables))["images"]]
    env.binding.runtime_revision = 2
    with pytest.raises(ImageGenerationError, match="不属于"):
        selected(env, variables, refs)


@pytest.mark.asyncio
async def test_private_recent_history_expires(ref_env):
    env = ref_env
    capture(env, query(picture("old"), kind="person"))
    env.clock[0] += env.refs.RECENT_TTL
    variables = capture(env, query(query_id=2, kind="person"))
    assert (await listing(env, variables, 2))["images"] == []


@pytest.mark.asyncio
async def test_history_rejects_future_and_out_of_window_sources(ref_env):
    env = ref_env
    variables = capture(env, query())
    env.plugin.call_platform_api.return_value = {
        "messages": [
            group_message(10),
            group_message(20, timestamp=NOW + 1),
            group_message(30, timestamp=NOW - 86401),
            group_message(1, timestamp=NOW),
        ]
    }
    result = await listing(env, variables)
    assert len(result["images"]) == 1
    assert (
        selected(env, variables, [result["images"][0]["image_ref"]])[0].url
        == picture("10").url
    )


@pytest.mark.asyncio
async def test_day_old_history_survives_short_lived_recent_upload_cache(ref_env):
    env = ref_env
    capture(env, query(picture("cached"), kind="person", query_id=2))
    env.clock[0] += env.refs.RECENT_TTL + 1
    variables = capture(env, query())
    env.plugin.call_platform_api.return_value = {
        "messages": [
            group_message(10, timestamp=NOW - 86400),
            group_message(11, timestamp=NOW - 7200),
        ]
    }
    result = await listing(env, variables)
    assert result["window_seconds"] == 86400
    assert result["max_messages"] == 1000
    assert not env.refs.STORE.recent
    assert [
        item.url
        for item in selected(
            env, variables, [row["image_ref"] for row in result["images"]]
        )
    ] == [picture("10").url, picture("11").url]


@pytest.mark.asyncio
async def test_thousand_messages_keep_only_latest_bounded_reference_metadata(ref_env):
    env = ref_env
    variables = capture(env, query())
    env.plugin.call_platform_api.return_value = {
        "messages": [
            group_message(number, timestamp=NOW - 1100 + number)
            for number in range(10, 1010)
        ]
    }
    result = await listing(env, variables)
    assert result["group_history_error"] is None
    assert len(result["images"]) == env.refs.MAX_CANDIDATES == 24
    assert result["history_messages"] == 1000
    assert result["candidates_truncated"] is True
    context = env.refs.STORE.get(env.plugin, variables, 1, "query-1")
    assert [item.image.url for item in context.refs.values()] == [
        picture(str(number)).url for number in range(986, 1010)
    ]
    assert len(context.refs) == 24
    env.plugin.call_platform_api.assert_awaited_once()


@pytest.mark.asyncio
async def test_history_settings_are_frozen_per_question_and_enforced(ref_env):
    env = ref_env
    config = {"history_hours": 2, "history_max_messages": 200}
    env.plugin.get_config = lambda: config
    variables = capture(env, query())
    config.update(history_hours=168, history_max_messages=1000)
    env.plugin.call_platform_api.return_value = {
        "messages": [
            group_message(10, timestamp=NOW - 7200),
            group_message(11, timestamp=NOW - 7201),
        ]
    }
    result = await listing(env, variables)
    assert len(result["images"]) == 1
    assert result["window_seconds"] == 7200
    assert result["max_messages"] == 200
    params = env.plugin.call_platform_api.await_args.args[2]["params"]
    assert params["window_seconds"] == 7200
    assert params["max_messages"] == 200
    assert params["before_message_id"] == "1" and params["before_time"] == NOW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        {"history_hours": 0, "history_max_messages": 99},
        {"history_hours": 169, "history_max_messages": 1001},
        {"history_hours": True, "history_max_messages": False},
        {"history_hours": "24", "history_max_messages": 100.5},
        None,
    ],
)
async def test_invalid_history_settings_use_bounded_defaults(ref_env, config):
    env = ref_env
    env.plugin.get_config = lambda: config
    result = await listing(env, capture(env, query()))
    assert result["window_seconds"] == 86400
    assert result["max_messages"] == 1000


@pytest.mark.asyncio
async def test_history_response_over_configured_count_keeps_current_source(ref_env):
    env = ref_env
    env.plugin.get_config = lambda: {"history_max_messages": 100}
    variables = capture(env, query(picture("current")))
    env.plugin.call_platform_api.return_value = {
        "messages": [group_message(number) for number in range(10, 111)]
    }
    result = await listing(env, variables)
    assert result["group_history_error"]
    assert len(result["images"]) == 1
    assert result["images"][0]["source"] == "current"


@pytest.mark.asyncio
async def test_partial_host_history_is_reported_without_retry_or_later_messages(ref_env):
    env = ref_env
    variables = capture(env, query())
    env.plugin.call_platform_api.return_value = {
        "messages": [group_message(10, timestamp=NOW - 7200)],
        "truncated": True,
    }
    result = await listing(env, variables)
    assert result["history_truncated"] is True
    assert result["history_messages"] == 1
    assert result["candidates_truncated"] is False
    env.plugin.call_platform_api.return_value = {
        "messages": [group_message(20, timestamp=NOW + 1)]
    }
    assert await listing(env, variables) == result
    env.plugin.call_platform_api.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_history_is_reported_and_not_retried_while_current_image_survives(
    ref_env,
):
    env = ref_env
    variables = capture(env, query(picture("current")))
    env.plugin.call_platform_api.side_effect = RuntimeError("private-url-and-token")
    first = await listing(env, variables)
    assert len(first["images"]) == 1
    assert first["group_history_error"]
    assert "private-url-and-token" not in json.dumps(first)
    assert await listing(env, variables) == first
    env.plugin.call_platform_api.assert_awaited_once()


@pytest.mark.asyncio
async def test_unavailable_file_image_is_visible_and_cannot_silently_select_another(
    ref_env,
):
    env = ref_env
    variables = capture(env, query())
    env.plugin.call_platform_api.return_value = {
        "messages": [
            {
                **group_message(10),
                "images": [],
                "unavailable_images": [
                    {"index": 1, "reason": "synthetic-private-error"},
                ],
            },
            group_message(11),
        ]
    }
    result = await listing(env, variables)
    assert len(result["images"]) == len(result["unavailable_images"]) == 1
    assert "image_ref" not in result["unavailable_images"][0]
    assert "不能" in result["unavailable_images"][0]["reason"]
    assert "synthetic-private-error" not in json.dumps(result)


@pytest.mark.asyncio
async def test_missing_first_picture_does_not_renumber_second_picture_as_first(ref_env):
    env = ref_env
    variables = capture(env, query())
    env.plugin.call_platform_api.return_value = {
        "messages": [
            group_message(
                10,
                images=[{"index": 5, "url": picture("second").url}],
                unavailable_images=[{"index": 2, "reason": "lookup_failed"}],
            ),
        ]
    }
    result = await listing(env, variables)
    assert result["images"][0]["message_image_index"] == 2
    assert result["unavailable_images"][0]["message_image_indices"] == [1]
    assert (
        result["images"][0]["message_ref"]
        == result["unavailable_images"][0]["message_ref"]
    )


@pytest.mark.asyncio
async def test_listing_rejects_missing_anchor_before_reading_group(ref_env):
    env = ref_env
    source = query()
    source.message_chain = MessageChain([])
    result = await listing(env, capture(env, source))
    assert "锚点" in result["group_history_error"]
    env.plugin.call_platform_api.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params", [{"group_id": "other"}, {"url": "https://example.com"}, None, []]
)
async def test_list_tool_rejects_model_supplied_scope_and_urls(ref_env, params):
    env = ref_env
    tool = env.listing.ListEditImageReferences()
    tool.plugin = env.plugin
    result = json.loads(await tool.call(params, None, 1, "query-1"))
    assert result["status"] == "error"
    env.plugin.call_platform_api.assert_not_awaited()
