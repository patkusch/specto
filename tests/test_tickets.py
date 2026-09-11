"""Ticket export tests: the Jira and Azure DevOps CSV files read back with the right rows and fields."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from specto.model import Analysis, Recording
from specto.tickets import (
    ADO_HEADERS,
    JIRA_HEADERS,
    export_ado_csv,
    export_jira_csv,
    export_tickets,
    slugify,
    trim,
)

FIXTURES = Path(__file__).parent / "fixtures"

TRICKY_STATEMENT = 'The system must show "Pending approval", not "Active", until a manager, or their deputy, approves.'


@pytest.fixture
def analysis() -> Analysis:
    return Analysis.model_validate(json.loads((FIXTURES / "sample_analysis.json").read_text()))


@pytest.fixture
def recording() -> Recording:
    return Recording.model_validate(json.loads((FIXTURES / "sample_recording.json").read_text()))


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def by_summary(rows: list[dict[str, str]], column: str, needle: str) -> dict[str, str]:
    matches = [r for r in rows if needle in r[column]]
    assert len(matches) == 1, f"expected one row with {needle!r} in {column}, got {len(matches)}"
    return matches[0]


# ------------------------------------------------------------------ Jira


class TestJira:
    @pytest.fixture
    def jira(self, analysis, recording, tmp_path):
        path = export_jira_csv(analysis, recording, tmp_path)
        assert path == tmp_path / "jira_import.csv"
        return path

    def test_header_and_row_count(self, jira, analysis):
        headers, rows = read_rows(jira)
        assert headers == JIRA_HEADERS
        assert len(rows) == len(analysis.requirements) + len(analysis.questions)

    def test_no_bom(self, jira):
        assert not jira.read_bytes().startswith(b"\xef\xbb\xbf")
        assert jira.read_bytes().startswith(b'"Issue Type"')

    def test_requirement_row(self, jira, analysis, recording):
        _, rows = read_rows(jira)
        r = next(r for r in analysis.requirements if r.id == "R002")
        row = by_summary(rows, "Issue ID", "R002")
        assert row["Issue Type"] == "Story"
        assert row["Summary"] == r.statement
        assert row["Priority"] == "Highest"
        assert row["Parent ID"] == ""
        desc = row["Description"]
        assert r.rationale in desc
        assert "h3. Acceptance criteria" in desc
        for ac in analysis.acceptance_criteria:
            if ac.requirement_id == "R002":
                assert ac.given in desc
                assert f"* *Given* {ac.given} *When* {ac.when} *Then* {ac.then}" in desc
        assert "h3. Source" in desc
        assert r.source_quote in desc
        assert "02:05" in desc
        assert "frame_0002.jpg" in desc

    def test_priority_mapping(self, jira):
        _, rows = read_rows(jira)
        assert by_summary(rows, "Issue ID", "R001")["Priority"] == "Highest"  # must
        assert by_summary(rows, "Issue ID", "R004")["Priority"] == "High"  # should

    def test_priority_mapping_could_and_unknown(self, analysis, recording, tmp_path):
        analysis.requirements[0].priority = "could"
        analysis.requirements[1].priority = "unknown"
        _, rows = read_rows(export_jira_csv(analysis, recording, tmp_path))
        assert by_summary(rows, "Issue ID", "R001")["Priority"] == "Medium"
        assert by_summary(rows, "Issue ID", "R002")["Priority"] == "Medium"

    def test_labels(self, jira):
        _, rows = read_rows(jira)
        labels = by_summary(rows, "Issue ID", "R002")["Labels"].split(" ")
        assert "specto" in labels
        assert "validation" in labels
        assert "customer-details" in labels
        assert all(label == label.lower() and " " not in label for label in labels)

    def test_question_row(self, jira, analysis):
        _, rows = read_rows(jira)
        q = next(q for q in analysis.questions if q.id == "Q004")
        row = by_summary(rows, "Issue ID", "Q004")
        assert row["Issue Type"] == "Task"
        assert row["Summary"] == f"Question: {q.question}"
        assert row["Parent ID"] == ""
        assert q.why_it_matters in row["Description"]
        assert q.context_quote in row["Description"]
        assert "04:02" in row["Description"]
        assert "frame_0004.jpg" in row["Description"]
        assert "permissions" in row["Labels"].split(" ")

    def test_comma_and_quote_round_trip(self, analysis, recording, tmp_path):
        analysis.requirements[0].statement = TRICKY_STATEMENT
        analysis.requirements[0].source_quote = 'He said "wait", then "go".'
        _, rows = read_rows(export_jira_csv(analysis, recording, tmp_path))
        row = by_summary(rows, "Issue ID", "R001")
        assert row["Summary"] == TRICKY_STATEMENT
        assert 'He said "wait", then "go".' in row["Description"]

    def test_long_summary_is_trimmed(self, analysis, recording, tmp_path):
        analysis.requirements[0].statement = "word " * 100
        _, rows = read_rows(export_jira_csv(analysis, recording, tmp_path))
        assert len(by_summary(rows, "Issue ID", "R001")["Summary"]) <= 250

    def test_every_field_quoted(self, jira):
        lines = jira.read_text(encoding="utf-8").splitlines()
        assert lines[0] == ",".join(f'"{h}"' for h in JIRA_HEADERS)
        assert lines[1].startswith('"Story","')
        # A multi-line Description is one quoted cell, so csv sees the same row count as rows written.
        with jira.open(encoding="utf-8", newline="") as fh:
            assert sum(1 for _ in csv.reader(fh)) < len(lines)


# ------------------------------------------------------------------ Azure DevOps


class TestAzureDevOps:
    @pytest.fixture
    def ado(self, analysis, recording, tmp_path):
        path = export_ado_csv(analysis, recording, tmp_path)
        assert path == tmp_path / "azure_devops_import.csv"
        return path

    def test_header_and_row_count(self, ado, analysis):
        headers, rows = read_rows(ado)
        assert headers == ADO_HEADERS
        assert len(rows) == len(analysis.requirements) + len(analysis.questions)

    def test_no_bom(self, ado):
        assert not ado.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_requirement_row(self, ado, analysis):
        _, rows = read_rows(ado)
        r = next(r for r in analysis.requirements if r.id == "R002")
        row = by_summary(rows, "Title", "reject a customer record")
        assert row["Work Item Type"] == "User Story"
        assert row["Title"] == r.statement
        assert row["Priority"] == "1"
        assert r.rationale in row["Description"]
        assert r.source_quote in row["Description"]
        assert "02:05" in row["Description"]
        assert "frame_0002.jpg" in row["Description"]
        assert row["Description"].startswith("<p>")
        ac_html = row["Acceptance Criteria"]
        assert ac_html.startswith("<ul><li>") and ac_html.endswith("</li></ul>")
        for ac in analysis.acceptance_criteria:
            if ac.requirement_id == "R002":
                assert ac.given in ac_html

    def test_priority_mapping(self, ado, analysis, recording, tmp_path):
        _, rows = read_rows(ado)
        assert by_summary(rows, "Title", "search for an existing customer")["Priority"] == "1"  # must
        assert by_summary(rows, "Title", "approvals queue until")["Priority"] == "2"  # should
        analysis.requirements[0].priority = "could"
        analysis.requirements[1].priority = "unknown"
        _, rows = read_rows(export_ado_csv(analysis, recording, tmp_path))
        assert by_summary(rows, "Title", "search for an existing customer")["Priority"] == "3"
        assert by_summary(rows, "Title", "reject a customer record")["Priority"] == "3"

    def test_tags(self, ado):
        _, rows = read_rows(ado)
        tags = by_summary(rows, "Title", "reject a customer record")["Tags"].split("; ")
        assert "specto" in tags
        assert "validation" in tags
        assert "customer-details" in tags

    def test_question_row(self, ado, analysis):
        _, rows = read_rows(ado)
        q = next(q for q in analysis.questions if q.id == "Q003")
        row = by_summary(rows, "Title", "Question: Which document types")
        assert row["Work Item Type"] == "Issue"
        assert row["Title"] == f"Question: {q.question}"
        assert q.why_it_matters in row["Description"]
        assert q.context_quote in row["Description"]
        assert "02:38" in row["Description"]
        assert "frame_0003.jpg" in row["Description"]
        assert row["Acceptance Criteria"] == ""
        assert "validation-rule" in row["Tags"].split("; ")

    def test_html_is_escaped(self, analysis, recording, tmp_path):
        analysis.requirements[0].rationale = "Values < 0 & > 100 are out of range."
        _, rows = read_rows(export_ado_csv(analysis, recording, tmp_path))
        row = by_summary(rows, "Title", "search for an existing customer")
        assert "&lt; 0 &amp; &gt; 100" in row["Description"]

    def test_comma_and_quote_round_trip(self, analysis, recording, tmp_path):
        analysis.requirements[0].statement = TRICKY_STATEMENT
        _, rows = read_rows(export_ado_csv(analysis, recording, tmp_path))
        assert by_summary(rows, "Title", "Pending approval")["Title"] == TRICKY_STATEMENT


# ------------------------------------------------------------------ both


def test_export_tickets_writes_both(analysis, recording, tmp_path):
    paths = export_tickets(analysis, recording, tmp_path)
    assert set(paths) == {"jira_csv", "ado_csv"}
    assert paths["jira_csv"] == tmp_path / "jira_import.csv"
    assert paths["ado_csv"] == tmp_path / "azure_devops_import.csv"
    assert all(p.exists() and p.stat().st_size > 0 for p in paths.values())


def test_export_tickets_creates_out_dir(analysis, recording, tmp_path):
    out = tmp_path / "nested" / "out"
    paths = export_tickets(analysis, recording, out)
    assert all(p.parent == out and p.exists() for p in paths.values())


def test_empty_analysis_still_writes_headers(recording, tmp_path):
    empty = Analysis(title="Nothing", summary="Nothing was found.")
    paths = export_tickets(empty, recording, tmp_path)
    for path, headers in ((paths["jira_csv"], JIRA_HEADERS), (paths["ado_csv"], ADO_HEADERS)):
        fieldnames, rows = read_rows(path)
        assert fieldnames == headers
        assert rows == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Customer search", "customer-search"),
        ("  Approvals   Queue! ", "approvals-queue"),
        ("missing information", "missing-information"),
        ("S04", "s04"),
    ],
)
def test_slugify(text, expected):
    assert slugify(text) == expected


def test_trim():
    assert trim("short") == "short"
    assert trim("a  b\n c") == "a b c"
    long = "x" * 300
    assert len(trim(long)) == 250
    assert trim(long).endswith("…")
