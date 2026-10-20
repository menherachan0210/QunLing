"""Strict LabelPlus 1.0 output, compatible with the original desktop importer."""

from __future__ import annotations

import json
import math
import re

MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_LABELS = 200
MAX_PAGE_CHARS = 16000
MAX_PAGE_BYTES = 64 * 1024
STRUCTURAL = re.compile(r"^\s*(?:>>>>>>>>\[|----------------\[)")


class TranslationError(Exception):
    """Safe, bounded diagnostic; never include upstream request bodies."""


def parse_page(content):
    if not isinstance(content, str) or len(content) > 64000:
        raise TranslationError("该页模型输出为空或超限。")
    content = content.strip()
    if content.startswith("```json") and content.endswith("```"):
        content = content[7:-3].strip()
    elif content.startswith("```") and content.endswith("```"):
        content = content[3:-3].strip()
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        raise TranslationError("该页模型未返回有效的文字定位与译文数据。") from None
    if not isinstance(data, dict) or data.get("status") not in {
        "ok",
        "no_text",
        "unreadable",
    }:
        raise TranslationError("该页模型输出状态无效。")
    if data["status"] == "unreadable":
        raise TranslationError("该页文字无法可靠辨认，需要人工检查原图。")
    labels = data.get("labels")
    if not isinstance(labels, list) or len(labels) > MAX_LABELS:
        raise TranslationError("该页文字条目无效或超过 200 条。")
    if (data["status"] == "ok" and not labels) or (
        data["status"] == "no_text" and labels
    ):
        raise TranslationError("该页文字状态与条目不一致。")
    cleaned = []
    for item in labels:
        if not isinstance(item, dict):
            raise TranslationError("该页文字条目格式无效。")
        for key in ("x", "y"):
            value = item.get(key)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise TranslationError("该页文字坐标超出图片范围。")
        if type(item.get("group")) is not int or item["group"] not in (1, 2):
            raise TranslationError("该页文字分组无效。")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise TranslationError("该页译文为空或单条超过 4000 字符。")
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if any(ord(c) < 32 and c not in "\n\t" for c in text):
            raise TranslationError("该页译文含无效控制字符。")
        if any(STRUCTURAL.match(line) for line in text.splitlines()):
            raise TranslationError("该页译文与 LabelPlus 结构标记冲突，需人工检查。")
        cleaned.append(
            {"x": item["x"], "y": item["y"], "group": item["group"], "text": text}
        )
    if sum(len(item["text"]) for item in cleaned) > MAX_PAGE_CHARS:
        raise TranslationError("该页译文总长度超过 16000 字符。")
    # Bound both the eventual TXT and checkpoint RPC before accepting a page.
    # 100 pages x 64 KiB leaves ample room inside the 8 MiB output/16 MiB wire cap.
    if (
        sum(len(item["text"].encode("utf-8")) + 100 for item in cleaned)
        > MAX_PAGE_BYTES
        or len(json.dumps(cleaned, ensure_ascii=False).encode("utf-8")) > MAX_PAGE_BYTES
    ):
        raise TranslationError("该页译文数据超过 64 KiB，需拆分图片。")
    return {"status": data["status"], "labels": cleaned}


def render_labelplus(pages):
    """Include failed pages as visible review notes, never silently omit pages."""
    lines = [
        "1,0",
        "-",
        "框内",
        "框外",
        "-",
        "AI 初译；请核对译文、漏字和标记位置。",
        "",
    ]
    seen = set()
    for page in pages:
        name = page["name"]
        if (
            not isinstance(name, str)
            or not name
            or name.casefold() in seen
            or "]<<<<<<<<" in name
            or any(c in name for c in "/\\\r\n\x00")
            or any(ord(c) < 32 for c in name)
        ):
            raise TranslationError("图片文件名不能安全写入 LabelPlus，或存在重名。")
        seen.add(name.casefold())
        lines.append(f">>>>>>>>[{name}]<<<<<<<<")
        result = page.get("result")
        if result is None:
            labels = [
                {
                    "x": 0.5,
                    "y": 0.5,
                    "group": 2,
                    "text": "【本页未完成翻译，请人工检查或重试】",
                }
            ]
        else:
            validated = parse_page(json.dumps(result, ensure_ascii=False))
            labels = validated["labels"]
        for i, label in enumerate(labels, 1):
            lines.append(
                f"----------------[{i}]----------------[{label['x']:.3f},{label['y']:.3f},{label['group']}]"
            )
            lines.append(label["text"])
            lines.append("")
        lines.append("")
    result = ("\r\n".join(lines).replace("\r\r\n", "\r\n") + "\r\n").encode("utf-8-sig")
    # Normalize multiline bodies as well as the format's own separators.
    result = (
        result.decode("utf-8-sig")
        .replace("\r\n", "\n")
        .replace("\n", "\r\n")
        .encode("utf-8-sig")
    )
    if len(result) > MAX_OUTPUT_BYTES:
        raise TranslationError("LabelPlus TXT 超过 8 MiB，请拆成较小压缩包。")
    return result


def output_name(zip_name, partial=False):
    stem = zip_name.rsplit(".", 1)[0]
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", stem).strip(" .")[:100] or "漫画"
    return stem + ("_待校对" if partial else "") + "_LabelPlus.txt"
