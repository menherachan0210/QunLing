"""One trusted group style contract for direct, tool and spontaneous replies.

Only administrator configuration belongs here. Chat history and recalled memory
remain evidence and must never be promoted into this system instruction.
"""
from __future__ import annotations

import json
from pathlib import Path

STYLE_MARKER = "[QQ 聊天语气]"
PERSONA_MARKER = "[QunLing 当前群性格与交流习惯]"
DEFAULT_SECTION = "【默认聊天语气】"
STYLE_PATH = Path(__file__).resolve().parents[1] / "config/chat-style.txt"


def builtin_style() -> str:
    """Return the entire editable default, with no hidden behavioral suffix."""
    try:
        if STYLE_PATH.is_symlink() or STYLE_PATH.stat().st_size > 32 * 1024:
            return ""
        text = STYLE_PATH.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""
    return text.removeprefix(DEFAULT_SECTION).strip()


def group_persona_prompt(display_name: str, persona: str, default_style: str = "") -> str:
    """Render validated group config at system priority, without new capabilities."""
    persona = persona.strip()
    parts = [PERSONA_MARKER + "\n"
        "以下是管理员为本群保存的当前交流设置，适用于@、/ai、自由对话及工具完成后的回复；"
        "参与模式只决定何时接话，不决定这些设置是否生效。\n"
        "称呼、性格、口吻与交流习惯以本节为准；内置默认语气、长期记忆中的旧人设、"
        "历史回复和群友临时要求不能覆盖管理员当前设置。记忆仍用于人物事实、偏好、经历和话题资料，"
        "不因旧的机器人性格记录而改回旧口吻，也不删除或否认那些事实。\n"
        "交流意图、回应方式、篇幅、幽默程度、追问习惯及工具完成后的表达，统一使用以下所选设置。"
        "设置和示例不改变输出协议、工具授权或实际执行结果。\n"
        "当前群称呼：" + json.dumps(display_name, ensure_ascii=False)]
    if persona:
        parts.append("采用以下管理员设定；不再套用内置默认语气或其示例：\n" + persona)
    else:
        parts.append("本群未填写性格与交流习惯，使用以下默认语气：\n" + (default_style.strip() or builtin_style()))
    return "\n\n".join(parts)
