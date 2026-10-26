"""A replay must test real generated continuations without contacting QQ."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dialogue_replay", ROOT / "scripts/Evaluate-Dialogue.py")
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


def test_check_only_needs_no_credentials_and_retains_multiturn_cases():
    cases = replay.load_cases(ROOT / "tests/fixtures/conversation_style_cases.json")
    assert {case["route"] for case in cases} == {"free", "direct", "research", "routing"}
    assert sum(bool(case.get("followups")) for case in cases) >= 5
    result = subprocess.run([sys.executable, str(ROOT / "scripts/Evaluate-Dialogue.py"), "--check-only"],
                            check=True, capture_output=True, text=True)
    status = json.loads(result.stdout)
    assert status["api_requests"] == status["qq_messages_sent"] == 0
    assert status["turns"] > status["cases"] == len(cases)


def test_interjection_cases_cover_silence_banter_real_help_and_custom_style():
    cases = replay.load_cases(ROOT / "tests/fixtures/proactive_interjection_cases.json")
    assert len([case for case in cases if "silence" in case.get("actions", [])]) >= 4
    assert any(case.get("followups") for case in cases)
    assert any(case.get("persona") for case in cases)
    assert any(case.get("media") for case in cases)
    assert any("task_request" in case.get("actions", []) for case in cases)


def test_report_escapes_model_text_and_retains_failures(tmp_path):
    path = tmp_path / "report.json"
    report = {"cases": [{"id": "<script>bad</script>", "criteria": "inspect",
                         "turns": [{"reply": "<img src=x onerror=bad()>", "error": "TimeoutError"}]}]}
    replay.write_report(path, report)
    assert json.loads(path.read_text(encoding="utf-8")) == report
    rendered = path.with_suffix(".html").read_text(encoding="utf-8")
    assert "<script>" not in rendered and "<img" not in rendered
    assert "TimeoutError" in rendered


def test_comparison_keeps_both_real_outputs_and_does_not_invent_a_quality_score(tmp_path):
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    for path, label in ((before, "old"), (after, "new")):
        path.write_text(json.dumps({"label": label, "cases": [{"id": "case", "criteria": "按语义审阅",
            "turns": [{"input": [{"sender_name": "成员甲", "sender_id": "a", "text": "原话"}],
                       "reply": label + "<script>alert(1)</script>"}]}]}, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "comparison.json"
    replay.compare_reports(before, after, output)
    assert set(json.loads(output.read_text(encoding="utf-8"))["mapping"]["case"].values()) == {"old", "new"}
    rendered = output.with_suffix(".html").read_text(encoding="utf-8")
    assert "old&lt;script&gt;" in rendered and "new&lt;script&gt;" in rendered
    assert "<script>" not in rendered and "quality_score" not in rendered
