"""JSON-compatible public file inspection, navigation and search API."""

from __future__ import annotations

import json
from pathlib import Path

from .archives import open_archive
from .core import DEFAULT_LIMITS, FileError, FileLimits, digest, integer, load_bytes, strict_keys
from .formats import MIMES, detect
from .cache import parse_cached as parse

_SELECTORS = {"section", "page", "sheet", "offset", "limit", "text_offset", "member",
              "column_offset", "column_limit", "cell"}


def _document(path, limits, member=None, *, password=None):
    raw = load_bytes(path, limits)
    name = Path(path).name
    if member is not None:
        if not isinstance(member, str):
            raise FileError("invalid_selector", "压缩包成员名称必须为字符串。")
        raw = open_archive(raw, limits, password=password).read(member)
        name = member
    fmt, warnings, archive = detect(raw, name, limits, password=password)
    sections, extra = parse(raw, fmt, limits, archive, password=password)
    return fmt, sorted(set(warnings + extra)), sections


def inspect_file(path, *, password: str | None = None, limits: FileLimits = DEFAULT_LIMITS):
    raw = load_bytes(path, limits)
    fmt, warnings, archive = detect(raw, Path(path).name, limits, password=password)
    structure, capabilities = [], ["transfer"]
    if fmt != "unknown":
        sections, extra = parse(raw, fmt, limits, archive, password=password)
        warnings = sorted(set(warnings + extra))
        for section in sections:
            item = {k: section[k] for k in ("id", "title", "page", "sheet", "needs_ocr",
                                           "needs_vision") if k in section}
            if "rows" in section:
                item["rows"] = len(section["rows"])
                item["columns"] = max((len(r["cells"]) for r in section["rows"]), default=0)
            if "entries" in section:
                item["members"] = len(section["entries"])
                item["expanded_bytes"] = sum(e["size"] for e in section["entries"])
            if "text" in section:
                item["characters"] = len(section["text"])
            structure.append(item)
        if fmt in {"zip", "rar", "7z"}:
            capabilities += ["list_members", "read_member"]
        elif fmt in {"png", "jpeg", "gif", "webp"}:
            capabilities += ["vision_required"]
        else:
            capabilities += ["read", "search"]
    expected = {"docx", "xlsx", "pptx", "pdf", "zip", "rar", "7z", "png", "jpeg", "jpg", "gif"}
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix == "jpg":
        suffix = "jpeg"
    if suffix in expected and suffix != fmt:
        warnings = sorted(set(warnings + ["extension_content_mismatch"]))
    return {"format": fmt, "mime": MIMES.get(fmt, "application/octet-stream"),
            "size": len(raw), "sha256": digest(raw), "capabilities": capabilities,
            "structure": structure, "warnings": warnings,
            "needs_ocr": any(item.get("needs_ocr", False) for item in structure)}


def read_archive_member(path, member: str, *, password: str | None = None,
                        limits: FileLimits = DEFAULT_LIMITS) -> bytes:
    """Read one exact validated member; caller registers the returned child asset."""
    return open_archive(load_bytes(path, limits), limits, password=password).read(member)


def read_file(path, selector=None, *, password: str | None = None,
              limits: FileLimits = DEFAULT_LIMITS):
    selector = {} if selector is None else selector
    strict_keys(selector, _SELECTORS)
    fmt, warnings, sections = _document(path, limits, selector.get("member"), password=password)
    offset = integer(selector.get("offset", 0))
    limit = integer(selector.get("limit", limits.max_page_items), 1, limits.max_page_items)
    text_offset = integer(selector.get("text_offset", 0), 0, limits.max_text_chars)
    selectors = [key for key in ("section", "page", "sheet") if key in selector]
    if len(selectors) > 1:
        raise FileError("invalid_selector", "一次只能按 section、page 或 sheet 之一选择。")
    explicit = bool(selectors)
    if explicit:
        key = selectors[0]
        if key == "page":
            integer(selector[key], 1, limits.max_sections)
        elif not isinstance(selector[key], str):
            raise FileError("invalid_selector", "结构定位符必须为字符串。")
        match_key = "id" if key == "section" else key
        matches = [(i, s) for i, s in enumerate(sections) if s.get(match_key) == selector[key]]
        if not matches:
            raise FileError("section_not_found", "没有找到指定的页、工作表或正文块。")
        position, section = matches[0]
    else:
        position = offset
        if position >= len(sections):
            return {"format": fmt, "sections": [], "next": None, "truncated": False,
                    "needs_ocr": False, "warnings": warnings}
        section = sections[position]
        offset = 0
    result = {k: v for k, v in section.items() if k not in {"text", "rows", "entries"}}
    next_selector = None
    base = {"section": section["id"]}
    if "member" in selector:
        base["member"] = selector["member"]
    # Escaping quotes/backslashes/newlines may double a text slice in JSON.
    budget = max(1, (limits.max_result_chars - 1500) // 2)
    if "cell" in selector:
        if "rows" not in section or not isinstance(selector["cell"], str):
            raise FileError("invalid_selector", "cell 仅用于表格，格式为 R1C1。")
        import re
        match = re.fullmatch(r"R([1-9][0-9]*)C([1-9][0-9]*)", selector["cell"])
        if not match:
            raise FileError("invalid_selector", "cell 格式为 R1C1。")
        row, column = map(int, match.groups())
        rows = section["rows"]
        if row > len(rows) or column > len(rows[row-1]["cells"]):
            raise FileError("section_not_found", "未找到指定单元格。")
        value = rows[row-1]["cells"][column-1]
        text = json.dumps(value, ensure_ascii=False, default=str)
        result["cell"] = selector["cell"]
        result["text"] = text[text_offset:text_offset + budget]
        result["text_offset"] = text_offset
        if text_offset + budget < len(text):
            next_selector = {**base, "cell": selector["cell"], "text_offset": text_offset + budget}
    elif "text" in section:
        text = section["text"]
        result["text"] = text[text_offset:text_offset + budget]
        result["text_offset"] = text_offset
        if text_offset + budget < len(text):
            next_selector = {**base, "text_offset": text_offset + budget}
    elif "rows" in section or "entries" in section:
        key = "rows" if "rows" in section else "entries"
        items = section[key]
        column_offset = integer(selector.get("column_offset", 0))
        column_limit = integer(selector.get("column_limit", 100), 1, limits.max_cells)
        chosen, used = [], 0
        columns_cut = False
        for item in items[offset:offset + limit]:
            if key == "rows":
                cells = item["cells"]
                columns_cut |= column_offset + column_limit < len(cells)
                item = {**item, "column_offset": column_offset,
                        "cells": cells[column_offset:column_offset + column_limit]}
            length = len(json.dumps(item, ensure_ascii=False, default=str))
            if used + length > budget:
                if not chosen:
                    raise FileError("result_limit", "单行过长；请缩小 column_limit 或使用 cell=R1C1 分页读取单元格。")
                break
            chosen.append(item)
            used += length
        result[key] = chosen
        result["offset"] = offset
        result["total_items"] = len(items)
        result["columns_truncated"] = columns_cut
        if columns_cut:
            warnings = sorted(set(warnings + ["columns_omitted_use_column_offset"]))
        if offset + len(chosen) < len(items):
            next_selector = {**base, "offset": offset + len(chosen), "limit": limit}
            if key == "rows":
                next_selector.update(column_offset=column_offset, column_limit=column_limit)
    if next_selector is None and "cell" not in selector and position + 1 < len(sections):
        next_selector = {"offset": position + 1, "limit": limit}
        if "member" in selector:
            next_selector["member"] = selector["member"]
    result_value = {"format": fmt, "sections": [result], "next": next_selector,
                    "truncated": bool(next_selector) or bool(result.get("columns_truncated")),
                    "needs_ocr": bool(section.get("needs_ocr")), "warnings": warnings}
    if len(json.dumps(result_value, ensure_ascii=False, default=str)) > limits.max_result_chars:
        raise FileError("result_limit", "读取结果超过单次输出限制，请缩小范围。")
    return result_value


def search_file(path, query: str, selector=None, *, limit: int = 20,
                password: str | None = None, limits: FileLimits = DEFAULT_LIMITS):
    if not isinstance(query, str) or not query.strip() or len(query) > 256:
        raise FileError("invalid_query", "检索词必须为 1–256 个字符的非空文本。")
    integer(limit, 1, 100)
    selector = {} if selector is None else selector
    strict_keys(selector, {"section", "page", "sheet", "member"})
    fmt, warnings, sections = _document(path, limits, selector.get("member"), password=password)
    hits, needle = [], query.casefold()
    more = False
    for section in sections:
        if any(section.get("id" if key == "section" else key) != value
               for key, value in selector.items() if key != "member"):
            continue
        units = [({"section": section["id"]}, section.get("text", ""))]
        for row in section.get("rows", []):
            for number, value in enumerate(row["cells"], 1):
                units.append(({"section": section["id"], "cell": f"R{row['row']}C{number}"},
                              json.dumps(value, ensure_ascii=False, default=str)))
        for entry in section.get("entries", []):
            units.append(({"member": entry["path"]}, entry["path"]))
        for location, text in units:
            index = text.casefold().find(needle)
            if index < 0:
                continue
            if len(hits) >= limit:
                more = True
                break
            hits.append({"location": location, "snippet": text[max(0, index-80):index+len(query)+160]})
        if more:
            break
    return {"format": fmt, "hits": hits, "truncated": more, "warnings": warnings}
