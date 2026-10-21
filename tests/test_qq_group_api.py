import asyncio
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from qunling.qq_group_api import read_group_api


def member(group="10", user="20"):
    return {"group_id": group, "user_id": user, "nickname": "测试", "card": "群名片",
            "role": "member", "private_field": "never forward"}


@pytest.mark.asyncio
async def test_only_current_public_member_fields_are_forwarded():
    bot = SimpleNamespace(call_action=AsyncMock(return_value=[member()]))
    result = await read_group_api(bot, "get_group_member_list", {"group_id": "10", "no_cache": True})
    assert set(result[0]) == {"group_id", "user_id", "nickname", "card", "role"}
    bot.call_action.assert_awaited_once_with("get_group_member_list", group_id="10", no_cache=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [member("11"), member(user="21"), {**member(), "role": "unknown"}])
async def test_rejects_cross_group_or_wrong_member_response(row):
    bot = SimpleNamespace(call_action=AsyncMock(return_value=row))
    with pytest.raises(RuntimeError, match="unavailable"):
        await read_group_api(bot, "get_group_member_info", {"group_id": "10", "user_id": "20"})


@pytest.mark.asyncio
@pytest.mark.parametrize("action,params", [
    ("set_group_card", {"group_id": "10"}),
    ("get_group_member_list", {"group_id": True}),
    ("get_group_member_list", {"group_id": "10", "url": "https://example.org"}),
    ("get_group_files_by_folder", {"group_id": "10", "folder_id": "bad\n"}),
    ("get_group_file_url", {"group_id": "10", "file_id": "file", "busid": True}),
])
async def test_invalid_requests_never_reach_qq(action, params):
    bot = SimpleNamespace(call_action=AsyncMock())
    with pytest.raises(ValueError):
        await read_group_api(bot, action, params)
    bot.call_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_directory_metadata_is_bounded_and_truncation_explicit():
    files = [{"group_id": "10", "file_id": str(i), "file_name": f"{i}.txt", "busid": 102,
              "file_size": 12, "url": "secret"} for i in range(2010)]
    bot = SimpleNamespace(call_action=AsyncMock(return_value={"files": files, "folders": []}))
    result = await read_group_api(bot, "get_group_root_files", {"group_id": "10"})
    assert result["truncated"] and len(result["files"]) == 2000
    assert "url" not in result["files"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{}, {"busid": 102}])
async def test_file_url_works_for_legacy_and_directory_import(extra):
    bot = SimpleNamespace(call_action=AsyncMock(return_value={"url": "https://example.org/file?sig=secret"}))
    params = {"group_id": "10", "file_id": "test", **extra}
    result = await read_group_api(bot, "get_group_file_url", params)
    bot.call_action.assert_awaited_once_with("get_group_file_url", **params)
    assert result == {"url": "https://example.org/file?sig=secret"}


@pytest.mark.asyncio
async def test_platform_failure_is_sanitized_and_cancellation_propagates():
    bot = SimpleNamespace(call_action=AsyncMock(side_effect=RuntimeError("secret download URL")))
    with pytest.raises(RuntimeError, match="^QQ group information is unavailable$"):
        await read_group_api(bot, "get_group_root_files", {"group_id": "10"})
    bot.call_action.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await read_group_api(bot, "get_group_root_files", {"group_id": "10"})


@pytest.mark.asyncio
async def test_long_filename_keeps_rejecting_length_instead_of_silently_truncating_extension():
    name = "a" * 300 + ".exe"
    bot = SimpleNamespace(call_action=AsyncMock(return_value={"folders": [], "files": [
        {"file_id": "file", "file_name": name, "busid": 102},
        {"file_id": "control", "file_name": "unsafe\n.txt", "busid": 102},
    ]}))
    result = await read_group_api(bot, "get_group_root_files", {"group_id": "10"})
    assert len(result["files"][0]["file_name"]) == 201
    assert result["files"][1]["file_name"] == "unsafe\n.txt"


@pytest.mark.asyncio
async def test_forward_action_delegates_to_validating_host_parser(monkeypatch):
    from qunling import qq_forward
    parser = AsyncMock(return_value={"cards": []})
    monkeypatch.setattr(qq_forward, "read_forward_context", parser)
    bot = SimpleNamespace(call_action=AsyncMock())
    params = {"group_id": "10", "anchor_message_id": "123", "before_time": 456}
    assert await read_group_api(bot, "get_group_forward_context", params) == {"cards": []}
    parser.assert_awaited_once_with(bot, params)
    bot.call_action.assert_not_awaited()
