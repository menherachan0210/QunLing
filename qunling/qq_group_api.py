"""Small read-only NapCat API surface; never expose raw endpoint payloads."""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit


READ_ACTIONS = frozenset({"get_group_member_info", "get_group_member_list",
                          "get_group_root_files", "get_group_files_by_folder", "get_group_file_url",
                          "get_group_forward_context"})


def qq_id(value):
    if type(value) not in (str, int):
        raise ValueError("Invalid QQ identity")
    value = str(value)
    if not value.isascii() or not value.isdecimal() or len(value) > 20 or int(value) <= 0:
        raise ValueError("Invalid QQ identity")
    return str(int(value))


def _identifier(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096
            or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("Invalid QQ resource identifier")
    return value


def _text(value, limit):
    return "".join(c for c in value[:limit] if c.isprintable()) if isinstance(value, str) else ""


def _number(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _member(row, group, user=None):
    if not isinstance(row, dict):
        raise ValueError("Invalid member response")
    uid = qq_id(row.get("user_id"))
    if ("group_id" in row and qq_id(row["group_id"]) != group) or (user and uid != user):
        raise ValueError("Mismatched member response")
    if row.get("role") not in {"owner", "admin", "member"}:
        raise ValueError("Invalid member role")
    return {"group_id": group, "user_id": uid, "nickname": _text(row.get("nickname"), 100),
            "card": _text(row.get("card"), 100), "role": row["role"]}


def _directory(raw, group):
    if not isinstance(raw, dict) or not isinstance(raw.get("files"), list) or not isinstance(raw.get("folders"), list):
        raise ValueError("Invalid group file directory")
    result = {"files": [], "folders": [], "truncated": bool(raw.get("truncated"))}
    remaining = 2000
    for kind, keys in (("folders", ("folder_id", "folder_name")), ("files", ("file_id", "file_name"))):
        result["truncated"] |= len(raw[kind]) > remaining
        for row in raw[kind][:remaining]:
            if not isinstance(row, dict):
                raise ValueError("Invalid directory entry")
            if "group_id" in row and qq_id(row["group_id"]) != group:
                raise ValueError("Mismatched directory group")
            name = row.get(keys[1])
            # Keep a rejecting sentinel length for oversized filenames; never
            # silently rename an input by dropping its extension or controls.
            name = name[:201] if kind == "files" and isinstance(name, str) else _text(name, 200)
            item = {keys[0]: _identifier(row.get(keys[0])), keys[1]: name}
            for key in ("file_size", "busid", "upload_time", "create_time", "total_file_count"):
                if key in row and _number(row[key]) is not None:
                    item[key] = row[key]
            result[kind].append(item)
        remaining -= len(result[kind])
    return result


async def read_group_api(bot, action, params):
    if action not in READ_ACTIONS or not isinstance(params, dict):
        raise ValueError("Unsupported group read action")
    if action == "get_group_forward_context":
        from qunling.qq_forward import read_forward_context
        return await read_forward_context(bot, params)
    required = {"group_id"}
    optional = set()
    if action == "get_group_member_info":
        required.add("user_id")
        optional.add("no_cache")
    elif action == "get_group_member_list":
        optional.add("no_cache")
    elif action == "get_group_files_by_folder":
        required.add("folder_id")
    elif action == "get_group_file_url":
        required.add("file_id")
        optional.add("busid")
    if not required <= params.keys() or params.keys() - required - optional:
        raise ValueError("Invalid group read parameters")
    group = qq_id(params["group_id"])
    payload = {"group_id": group}
    for key in required - {"group_id"}:
        payload[key] = qq_id(params[key]) if key == "user_id" else _identifier(params[key])
    if "no_cache" in params:
        if type(params["no_cache"]) is not bool:
            raise ValueError("Invalid cache parameter")
        payload["no_cache"] = params["no_cache"]
    if "busid" in params:
        if _number(params["busid"]) is None:
            raise ValueError("Invalid file bus identifier")
        payload["busid"] = params["busid"]
    try:
        async with asyncio.timeout(12):
            result = await bot.call_action(action, **payload)
        if action == "get_group_member_info":
            return _member(result, group, payload["user_id"])
        if action == "get_group_member_list":
            if not isinstance(result, list) or len(result) > 5000:
                raise ValueError("Invalid group member list")
            return [_member(row, group) for row in result]
        if action in {"get_group_root_files", "get_group_files_by_folder"}:
            return _directory(result, group)
        url = result.get("url") if isinstance(result, dict) else None
        if (not isinstance(url, str) or not 1 <= len(url) <= 8192
                or any(ord(c) <= 32 for c in url) or "\\" in url):
            raise ValueError("Invalid download URL")
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment):
            raise ValueError("Invalid download URL")
        _ = parsed.port
        return {"url": url}
    except Exception:
        # Network exceptions can carry signed URLs and filenames.
        raise RuntimeError("QQ group information is unavailable") from None


def install(mapping):
    for action in READ_ACTIONS:
        async def handler(bot, params, _action=action):
            return await read_group_api(bot, _action, params)
        mapping[action] = handler
