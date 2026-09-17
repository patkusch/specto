"""Tests for comparing two finished analyses of the same journey."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from specto.cli import main
from specto.compare import compare_analyses, compare_html, compare_report_markdown
from specto.model import Analysis, Recording

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def analysis_a() -> Analysis:
    return Analysis.model_validate(json.loads((FIXTURES / "sample_analysis.json").read_text()))


def build_analysis_b(base: dict) -> Analysis:
    """An 'after' reading of the same journey: one item of each kind dropped,
    one added, and one reworded closely enough to still be the same item."""
    b = copy.deepcopy(base)

    # Screen S02 renamed (a rewording, not a different screen).
    details = next(s for s in b["screens"] if s["id"] == "S02")
    details["name"] = "Customer Details (v2)"

    # Requirement R001 reworded; R004 dropped; a brand new one added.
    r001 = next(r for r in b["requirements"] if r["id"] == "R001")
    r001["statement"] = r001["statement"].rstrip(".") + " in the system."
    b["requirements"] = [r for r in b["requirements"] if r["id"] != "R004"]
    b["requirements"].append({
        "id": "R010",
        "statement": "The system must send an email notification to the customer once their record is approved.",
        "rationale": None, "source_quote": "We email them once it is through.",
        "timestamp": 260.0, "keyframe_index": 5, "screen_id": "S04",
        "kind": "workflow", "priority": "should", "confidence": "medium",
    })

    # Question Q002 reworded; Q003 dropped; a brand new one added.
    q002 = next(q for q in b["questions"] if q["id"] == "Q002")
    q002["question"] = "Is the email address mandatory, or can a customer be onboarded with no email at all?"
    b["questions"] = [q for q in b["questions"] if q["id"] != "Q003"]
    b["questions"].append({
        "id": "Q020",
        "question": "Does the manager get a reminder if the approval sits for more than a day?",
        "why_it_matters": "Decides whether a stale approval is ever chased up.",
        "context_quote": None, "timestamp": 262.0, "keyframe_index": 5,
        "screen_id": "S04", "category": "other",
    })

    b["title"] = "Second reading"
    b["summary"] = "A second walkthrough of the same onboarding journey."
    return b


@pytest.fixture
def analysis_b(analysis_a) -> Analysis:
    return Analysis.model_validate(build_analysis_b(json.loads((FIXTURES / "sample_analysis.json").read_text())))


# ------------------------------------------------------------------ compare_analyses


def test_removed_items_classified(analysis_a, analysis_b):
    comparison = compare_analyses(analysis_a, analysis_b, label_a="before", label_b="after")
    removed_ids = {(i.kind, i.id) for i in comparison.removed}
    assert ("requirement", "R004") in removed_ids
    assert ("question", "Q003") in removed_ids


def test_added_items_classified(analysis_a, analysis_b):
    comparison = compare_analyses(analysis_a, analysis_b)
    added_ids = {(i.kind, i.id) for i in comparison.added}
    assert ("requirement", "R010") in added_ids
    assert ("question", "Q020") in added_ids


def test_changed_items_classified_with_both_versions(analysis_a, analysis_b):
    comparison = compare_analyses(analysis_a, analysis_b)
    changed_by_kind_id = {(i.kind, i.id_a): i for i in comparison.changed}
    req = changed_by_kind_id[("requirement", "R001")]
    assert req.text_a.endswith("can be created.")
    assert req.text_b.endswith("can be created in the system.")
    assert req.id_b == "R001"

    screen = changed_by_kind_id[("screen", "S02")]
    assert screen.text_a == "Customer details"
    assert screen.text_b == "Customer Details (v2)"

    question = changed_by_kind_id[("question", "Q002")]
    assert question.text_a != question.text_b
    assert "email" in question.text_a and "email" in question.text_b


def test_unchanged_items_are_excluded_from_every_list(analysis_a, analysis_b):
    comparison = compare_analyses(analysis_a, analysis_b)
    every_id = ({(i.kind, i.id) for i in comparison.added} |
                {(i.kind, i.id) for i in comparison.removed} |
                {(i.kind, i.id_a) for i in comparison.changed} |
                {(i.kind, i.id_b) for i in comparison.changed})
    # R002 and R003 were not touched, so neither should appear anywhere.
    assert ("requirement", "R002") not in every_id
    assert ("requirement", "R003") not in every_id
    # Every field and action was left alone.
    assert not [i for i in comparison.added + comparison.removed if i.kind in ("field", "action")]
    assert not [i for i in comparison.changed if i.kind in ("field", "action")]


def test_comparison_is_labelled(analysis_a, analysis_b):
    comparison = compare_analyses(analysis_a, analysis_b, label_a="pre-migration", label_b="post-migration")
    assert comparison.label_a == "pre-migration"
    assert comparison.label_b == "post-migration"


def test_identical_analyses_have_no_differences(analysis_a):
    same = Analysis.model_validate(analysis_a.model_dump())
    comparison = compare_analyses(analysis_a, same)
    assert comparison.total == 0
    assert comparison.added == comparison.removed == comparison.changed == []


# ------------------------------------------------------------------ reports


def test_markdown_report_cites_source_and_sections(analysis_a, analysis_b):
    comparison = compare_analyses(analysis_a, analysis_b, label_a="before", label_b="after")
    text = compare_report_markdown(comparison)
    assert "# Comparing before with after" in text
    assert "## Added in after" in text
    assert "## Removed from before" in text
    assert "## Changed" in text
    assert "R010" in text and "R004" in text and "R001" in text
    assert "before **R001**" in text and "after **R001**" in text


def test_markdown_no_differences_language(analysis_a):
    same = Analysis.model_validate(analysis_a.model_dump())
    comparison = compare_analyses(analysis_a, same)
    text = compare_report_markdown(comparison)
    assert "No differences found." in text


def test_html_report_cites_source(analysis_a, analysis_b):
    # Plain static HTML5, in the same style as html_report.py, not strict XHTML
    # like the Confluence/SharePoint exports; that's why this checks structure
    # and content rather than parsing it as XML.
    comparison = compare_analyses(analysis_a, analysis_b, label_a="before", label_b="after")
    page = compare_html(comparison)
    assert page.count("<html") == 1 and page.count("</html>") == 1
    assert "<script" not in page.lower()
    assert "R010" in page and "R004" in page and "R001" in page
    assert "Comparing before with after" in page


def test_html_no_differences_language(analysis_a):
    same = Analysis.model_validate(analysis_a.model_dump())
    comparison = compare_analyses(analysis_a, same)
    page = compare_html(comparison)
    assert "No differences found." in page


# ------------------------------------------------------------------ CLI


VTT = """WEBVTT

00:00:00.000 --> 00:00:02.500
<v Sam>This is the customer search screen, we type the postcode here.

00:00:03.200 --> 00:00:05.800
<v Sam>Then we open the customer details and check the account number.

00:00:06.100 --> 00:00:08.700
<v Sam>Save sends it to the approval queue, only a team lead can approve.

00:00:09.300 --> 00:00:11.500
<v Sam>And that is it, the customer is done.
"""


def test_cli_compare_on_two_fake_runs(synthetic_video: Path, tmp_path: Path) -> None:
    vtt = tmp_path / "w.vtt"
    vtt.write_text(VTT, encoding="utf-8")
    a, b = tmp_path / "a", tmp_path / "b"
    assert main(["run", str(synthetic_video), "--transcript", str(vtt), "--out", str(a), "--fake"]) == 0
    assert main(["run", str(synthetic_video), "--transcript", str(vtt), "--out", str(b), "--fake"]) == 0

    out = tmp_path / "diff"
    rc = main(["compare", str(a), str(b), "--out", str(out), "--label-a", "run a", "--label-b", "run b"])
    assert rc == 0

    md_path, html_path = out / "compare.md", out / "compare.html"
    assert md_path.exists() and html_path.exists()
    md_text = md_path.read_text(encoding="utf-8")
    html_text = html_path.read_text(encoding="utf-8")

    # --fake is deterministic: the same recording read twice yields the same
    # analysis, so there is nothing to report. If that ever stops being true,
    # this assertion is the signal to update it.
    a_analysis = Analysis.model_validate_json((a / "analysis.json").read_text())
    b_analysis = Analysis.model_validate_json((b / "analysis.json").read_text())
    if a_analysis == b_analysis:
        assert "No differences found." in md_text
        assert "No differences found." in html_text
    else:
        assert any(marker in md_text for marker in ("## Added in run b", "## Removed from run a", "## Changed"))
        assert any(item.id in md_text for item in
                   compare_analyses(a_analysis, b_analysis, "run a", "run b").added +
                   compare_analyses(a_analysis, b_analysis, "run a", "run b").removed) or \
               compare_analyses(a_analysis, b_analysis, "run a", "run b").changed


def test_cli_compare_default_out_dir(synthetic_video: Path, tmp_path: Path) -> None:
    vtt = tmp_path / "w.vtt"
    vtt.write_text(VTT, encoding="utf-8")
    a, b = tmp_path / "a", tmp_path / "b"
    assert main(["run", str(synthetic_video), "--transcript", str(vtt), "--out", str(a), "--fake"]) == 0
    assert main(["run", str(synthetic_video), "--transcript", str(vtt), "--out", str(b), "--fake"]) == 0
    assert main(["compare", str(a), str(b)]) == 0
    default_out = a.parent / f"{a.name}-vs-{b.name}"
    assert (default_out / "compare.md").exists()
    assert (default_out / "compare.html").exists()
