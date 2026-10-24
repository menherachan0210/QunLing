"""Managed defaults cannot compete with an administrator's group persona."""
from qunling import group_persona


def test_custom_persona_excludes_default_tone_but_retains_evidence_rules():
    prompt = group_persona.group_persona_prompt("冰夜姬", "说话自信、直截了当，可以轻松反驳。")
    assert "说话自信、直截了当，可以轻松反驳。" in prompt
    assert "会议全勤奖" not in prompt
    assert "可以有自己的判断和一点幽默" not in prompt
    assert "不因为对方语气强、人数多或要求道歉就改口" in prompt
    assert "不能把没做成的事说成做成" in prompt
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
    assert "不能把没做成的事说成做成" in prompt
    own = group_persona.group_persona_prompt("乙", "开朗自信", custom)
    assert "开朗自信" in own and custom not in own
    assert group_persona.builtin_style() in group_persona.group_persona_prompt("甲", "", "")
