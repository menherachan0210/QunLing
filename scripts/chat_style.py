"""Versioned conversation style shared by initial setup and later adjustments."""

from copy import deepcopy
from pathlib import Path

MARKER = "[QQ 聊天语气]"
STYLE_PATH = Path(__file__).resolve().parents[1] / "config/chat-style.txt"


def styled_prompts(prompts):
    """Replace only our style block, preserving functional and user prompts."""
    result = [
        deepcopy(message)
        for message in prompts
        if not (
            message.get("role") == "system"
            and isinstance(message.get("content"), str)
            and message["content"].startswith(MARKER)
        )
    ]
    result.append(
        {
            "role": "system",
            "content": MARKER + "\n以下是内置默认语气；本群另有管理员性格设置时，以当前群设置为准。"
                       "这是完整的默认设置，没有额外隐藏的交流习惯。\n" + STYLE_PATH.read_text(encoding="utf-8").strip(),
        }
    )
    return result
