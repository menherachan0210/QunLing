"""Strict administrator settings for bounded public-source collection."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urldefrag

from qunling.net_errors import NetworkError
from qunling.network import validate_public_url
from qunling.scope import Scope


class ConfigError(ValueError):
    pass


def defaults():
    return {"revision": 0, "enabled": True, "search_ttl": 300, "page_ttl": 3600,
            "daily_budget": 96, "sources": []}


def validate(value):
    if not isinstance(value, dict) or set(value) != set(defaults()):
        raise ConfigError("知识库设置字段不完整或含不支持的字段。")
    result = dict(value)
    if type(value["enabled"]) is not bool:
        raise ConfigError("知识库开关无效。")
    for name, low, high in (("revision", 0, 2**53 - 1), ("search_ttl", 30, 3600),
                             ("page_ttl", 60, 604800), ("daily_budget", 1, 500)):
        if type(value[name]) is not int or not low <= value[name] <= high:
            raise ConfigError(f"{name} 必须为 {low} 至 {high} 的整数。")
    sources = value["sources"]
    if not isinstance(sources, list) or len(sources) > 32:
        raise ConfigError("最多配置 32 个公开来源。")
    result["sources"], seen = [], set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"group_id", "url", "interval", "ttl"}:
            raise ConfigError("每个来源需包含群号、URL、刷新秒数和有效秒数。")
        group = source["group_id"]
        if not isinstance(group, str) or not re.fullmatch(r"[1-9][0-9]{0,31}", group):
            raise ConfigError("来源群号无效。")
        for key in ("interval", "ttl"):
            if type(source[key]) is not int or not 60 <= source[key] <= 604800:
                raise ConfigError("刷新间隔和有效期应为 60 至 604800 秒。")
        if not isinstance(source["url"], str):
            raise ConfigError("采集来源必须是公开 HTTP(S) 网页地址。")
        try:
            url = validate_public_url(urldefrag(source["url"])[0])
        except (TypeError, ValueError, NetworkError):
            raise ConfigError("采集来源必须是公开 HTTP(S) 网页地址。") from None
        if (group, url) in seen:
            raise ConfigError("同一群不能重复配置同一个来源。")
        seen.add((group, url))
        result["sources"].append({**source, "url": url})
    return result


def bot_key(scope: Scope):
    return hashlib.sha256(json.dumps([scope.workspace_id, scope.bot_id]).encode()).hexdigest()


def public_scope(scope: Scope):
    return Scope(scope.workspace_id, scope.bot_id, scope.conversation_id, "*")


def requires_live(text):
    return bool(re.search(r"最新|实时|今天|今日|现在|当前|刚刚|联网|上网|核实|查证|价格|天气|股价|汇率|"
                          r"\b(?:latest|live|today|current|verify|price|weather)\b", str(text)[:3000], re.I))
