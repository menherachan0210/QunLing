"""Managed defaults cannot compete with an administrator's group persona."""
from qunling import group_persona


def test_custom_persona_replaces_the_entire_default_without_hidden_behavior():
    prompt = group_persona.group_persona_prompt("冰夜姬", "说话自信、直截了当，可以轻松反驳。")
    assert "说话自信、直截了当，可以轻松反驳。" in prompt
    assert "会议全勤奖" not in prompt
    assert "可以有自己的判断和一点幽默" not in prompt
    assert "不因为对方语气强、人数多或要求道歉就改口" not in prompt
    assert "不能把没做成的事说成做成" not in prompt
    assert "不改变输出协议、工具授权" in prompt
    assert "长期记忆中的旧人设" in prompt


def test_empty_persona_uses_shipped_default_and_keeps_group_name():
    prompt = group_persona.group_persona_prompt("本群小助手", "  ")
    assert '当前群称呼："本群小助手"' in prompt
    assert "会议全勤奖" in prompt
    assert "未经核实不能声称已经证实" in prompt


def test_missing_or_legacy_style_file_does_not_suppress_group_setting(tmp_path, monkeypatch):
    source = tmp_path / "chat-style.txt"
    monkeypatch.setattr(group_persona, "STYLE_PATH", source)
    assert "自信爽快" in group_persona.group_persona_prompt("甲", "自信爽快")
    source.write_text("旧版本整段默认语气", encoding="utf-8")
    assert "旧版本整段默认语气" not in group_persona.group_persona_prompt("甲", "自信爽快")
    assert "旧版本整段默认语气" in group_persona.group_persona_prompt("甲", "")


def test_saved_default_applies_only_to_groups_without_independent_persona():
    custom = "正式克制，充分展开分析，不讲笑话。"
    prompt = group_persona.group_persona_prompt("甲", "", custom)
    assert custom in prompt and "会议全勤奖" not in prompt
    assert "不能把没做成的事说成做成" not in prompt
    own = group_persona.group_persona_prompt("乙", "开朗自信", custom)
    assert "开朗自信" in own and custom not in own
    assert group_persona.builtin_style() in group_persona.group_persona_prompt("甲", "", "")


def test_all_default_sections_are_visible_and_admin_replacement_is_complete(tmp_path, monkeypatch):
    source = tmp_path / "chat-style.txt"
    source.write_text("【默认聊天语气】\n语气甲\n【对话依据与任务规则】\n习惯乙", encoding="utf-8")
    monkeypatch.setattr(group_persona, "STYLE_PATH", source)
    assert "习惯乙" in group_persona.builtin_style()
    assert group_persona.builtin_style() in group_persona.group_persona_prompt("甲", "", "")
    configured = group_persona.group_persona_prompt("甲", "", "新的默认设置")
    assert "新的默认设置" in configured and "习惯乙" not in configured and "语气甲" not in configured
