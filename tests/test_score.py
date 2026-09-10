"""Scorer tests: exact numbers on the fixture answer key, matching rules, traceability, exit codes."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from specto.model import Analysis
from specto.score import ScoreReport, format_report, main, matches, score, score_dir

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def analysis() -> Analysis:
    return Analysis.model_validate_json((FIXTURES / "sample_analysis.json").read_text())


@pytest.fixture
def expected() -> dict:
    return json.loads((FIXTURES / "sample_expected.json").read_text())


@pytest.fixture
def report(analysis, expected) -> ScoreReport:
    return score(analysis, expected)


@pytest.fixture
def out_dir(tmp_path) -> Path:
    shutil.copy(FIXTURES / "sample_analysis.json", tmp_path / "analysis.json")
    return tmp_path


# ------------------------------------------------------------- fixture numbers


def test_per_category_numbers(report):
    c = report.categories
    assert list(c) == ["screens", "fields", "actions", "requirements", "questions"]
    assert (c["screens"].expected, c["screens"].found, c["screens"].recall) == (5, 4, 0.8)
    assert (c["fields"].expected, c["fields"].found, c["fields"].recall) == (8, 8, 1.0)
    assert (c["actions"].expected, c["actions"].found, c["actions"].recall) == (5, 5, 1.0)
    assert (c["requirements"].expected, c["requirements"].found, c["requirements"].recall) == (5, 4, 0.8)
    assert (c["questions"].expected, c["questions"].found, c["questions"].recall) == (5, 4, 0.8)
    assert report.overall_recall == pytest.approx(0.88)


def test_missed_items_keep_their_original_spec(report):
    c = report.categories
    assert c["screens"].missed == ["dashboard"]
    assert c["fields"].missed == []
    assert c["actions"].missed == []
    assert c["requirements"].missed == [["audit log|audit trail"]]
    assert c["questions"].missed == [["password", "reset"]]


def test_extras_counted(report):
    c = report.categories
    # Title and Last name are in the analysis but not in the answer key.
    assert c["fields"].extras == 2
    assert c["screens"].extras == 0
    assert c["actions"].extras == 0
    assert c["requirements"].extras == 0
    assert c["questions"].extras == 0


def test_fixture_is_fully_traceable(report):
    t = report.traceability
    assert t.violations == 0


# ------------------------------------------------------------- matching rules


def test_alternatives_and_case():
    assert matches("Is the email address MANDATORY?", ["mandatory|required"])
    assert matches("Is the email address required?", ["mandatory|required"])
    assert not matches("Is the email address optional?", ["mandatory|required"])
    assert matches("Postcode must be a valid UK format", ["postcode", "uk|format|valid"])
    assert not matches("Postcode is shown", ["postcode", "uk|format|valid"])


def test_alternatives_on_the_analysis(analysis):
    hit = score(analysis, {"questions": [["email", "mandatory|required"]]})
    miss = score(analysis, {"questions": [["email", "compulsory|obligatory"]]})
    assert hit.categories["questions"].found == 1
    assert miss.categories["questions"].found == 0
    assert miss.categories["questions"].missed == [["email", "compulsory|obligatory"]]


def test_field_is_scoped_to_its_screen(analysis):
    right = score(analysis, {"fields": [{"screen": "customer search", "label": "postcode"}]})
    wrong = score(analysis, {"fields": [{"screen": "customer details", "label": "postcode"}]})
    assert right.categories["fields"].found == 1
    assert wrong.categories["fields"].found == 0
    assert wrong.categories["fields"].missed == [{"screen": "customer details", "label": "postcode"}]
    # A field label without a screen matches on any screen.
    anywhere = score(analysis, {"fields": [{"label": "postcode"}]})
    assert anywhere.categories["fields"].found == 1


def test_each_analysis_item_satisfies_at_most_one_expected(analysis):
    # R002 mentions both the email and the age check; the second expected item cannot reuse it.
    report = score(analysis, {"requirements": [["email", "valid"], ["eighteen|under 18"]]})
    assert report.categories["requirements"].found == 1
    assert report.categories["requirements"].missed == [["eighteen|under 18"]]


def test_overall_ignores_categories_with_nothing_expected(analysis):
    report = score(analysis, {"screens": ["customer search"], "actions": []})
    assert report.categories["actions"].recall == 0.0
    assert report.categories["actions"].extras == len(analysis.actions)
    assert report.overall_recall == 1.0
    assert score(analysis, {}).overall_recall == 0.0


def test_unknown_category_is_an_error(analysis):
    with pytest.raises(ValueError, match="unknown categories"):
        score(analysis, {"screen": ["customer search"]})


# --------------------------------------------------------------- traceability


def test_traceability_violations_detected(analysis, expected):
    broken = analysis.model_copy(deep=True)
    broken.requirements[0].source_quote = "   "
    broken.acceptance_criteria[0].requirement_id = "R999"
    broken.acceptance_criteria[1].requirement_id = "R998"
    broken.fields[0].keyframe_index = -1
    broken.screens[0].keyframe_indexes = [-3]
    t = score(broken, expected).traceability
    assert t.requirements_without_source_quote == 1
    assert t.criteria_with_unknown_requirement == 2
    assert t.negative_keyframe_indexes == 2
    assert t.violations == 5


# ------------------------------------------------------------------ reporting


def test_format_report_lines(report):
    text = format_report(report)
    lines = text.splitlines()
    assert lines[0].split() == ["Category", "Expected", "Found", "Recall", "Extras"]
    assert lines[1].split() == ["screens", "5", "4", "0.80", "0"]
    assert lines[2].split() == ["fields", "8", "8", "1.00", "2"]
    assert "Missed:" in lines
    assert "  screens: dashboard" in lines
    assert '  requirements: ["audit log|audit trail"]' in lines
    assert "  requirements without source quote: 0" in lines
    assert lines[-1] == "Overall recall: 0.88"
    assert "\x1b[" not in text


def test_score_dir_writes_score_json(out_dir):
    report = score_dir(out_dir, FIXTURES / "sample_expected.json")
    saved = ScoreReport.model_validate_json((out_dir / "score.json").read_text())
    assert saved == report
    assert saved.overall_recall == pytest.approx(0.88)


def test_main_exit_codes(out_dir, capsys):
    args = [str(out_dir), str(FIXTURES / "sample_expected.json")]
    assert main(args) == 0  # default --min 0.7, fixture scores 0.88
    assert main(args + ["--min", "0.88"]) == 0
    assert main(args + ["--min", "0.9"]) == 1
    out = capsys.readouterr().out
    assert "Overall recall: 0.88" in out
    assert "FAIL" in out
