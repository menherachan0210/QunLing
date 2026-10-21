"""Share only bounded public query conclusions, never resource capabilities."""
from __future__ import annotations

import asyncio

from qunling.integration import workspace_key
from qunling.group_knowledge import publish


def _name(member):
    return str(member.get("card") or member.get("nickname") or member.get("user_id") or "本群成员")[:100]


def _avatar_subject(target):
    if target.get("kind") == "group":
        return "本群头像"
    user_id = target.get("user_id")
    if type(user_id) in (str, int):
        user_id = str(user_id)
        if user_id.isascii() and user_id.isdigit() and 1 <= len(user_id) <= 20 and int(user_id) > 0:
            # Names are not identities. Keep all four verified targets within
            # the shared cache's 160-character subject budget, including IDs.
            return f"{_name(target)[:8]}（QQ{int(user_id)}）的头像"
    return _name(target)[:30] + "的头像"


async def publish_result(plugin, context, action, result):
    if not isinstance(result, dict) or result.get("status") != "ok":
        return
    source, subject, text = "", "", ""
    if action == "members":
        members = result.get("members", [result["member"]] if isinstance(result.get("member"), dict) else [])
        if isinstance(members, list):
            rows = [row for row in members[:8] if isinstance(row, dict)]
            subject = "、".join(_name(row) for row in rows)[:200]
            text = "；".join(f"{_name(row)}：昵称{str(row.get('nickname', ''))[:100]}，"
                            f"群身份{row.get('role', '未知')}" for row in rows)
            source = "member_info"
    elif action == "avatar":
        targets = result.get("targets") or [result.get("target", {})]
        subject = "、".join(_avatar_subject(row)
                           for row in targets[:4] if isinstance(row, dict))[:200]
        text = str(result.get("analysis", ""))[:1100]
        source = "avatar_analysis"
    elif action == "group_files" and isinstance(result.get("entries"), list):
        rows = [row for row in result["entries"][:12] if isinstance(row, dict)]
        subject = "群文件目录"
        text = "本次查看的部分目录条目：" + "；".join(str(row.get("name", ""))[:100] for row in rows)
        source = "group_file_list"
    if source and text:
        try:
            await asyncio.to_thread(publish, workspace_id=workspace_key(plugin), bot_id=context.bot,
                                    group_id=context.group, question=context.current_question,
                                    source=source, subject=subject, text=text,
                                    message_ids=[context.anchor], actor_id=context.sender)
        except Exception:
            # Optional short-term recall must not turn a successful read into a
            # failed tool or cause the model to repeat paid vision work.
            pass
