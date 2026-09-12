"""The glossary: terms lifted from the analysis, traced to the rows that use them,
and a naming check that catches near-duplicate spellings."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from specto.glossary import (
    GLOSSARY_HEADERS,
    GlossaryEntry,
    build_glossary,
    consistency_findings,
    control_name,
    glossary_markdown,
    glossary_rows,
    glossary_with_findings,
    role_in_statement,
)
from specto.model import (
    AcceptanceCriterion,
    Analysis,
    DataField,
    Requirement,
    Screen,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def analysis() -> Analysis:
    return Analysis.model_validate(json.loads((FIXTURES / "sample_analysis.json").read_text()))


@pytest.fixture
def entries(analysis: Analysis) -> list[GlossaryEntry]:
    return build_glossary(analysis)


def by_term(entries: list[GlossaryEntry]) -> dict[str, GlossaryEntry]:
    return {e.term: e for e in entries}


# ------------------------------------------------------------------ the fixture


def test_fixture_terms_have_the_right_kinds(entries):
    kinds = {e.term: e.kind for e in entries}
    assert kinds["Onboarding clerk"] == "role"
    assert kinds["Team manager"] == "role"
    for screen in ("Customer search", "Customer details", "Identity check", "Approvals queue"):
        assert kinds[screen] == "screen"
    for field in ("Postcode", "Surname", "Title", "First name", "Last name", "Date of birth", "Email",
                  "ID document", "Verification result", "Status"):
        assert kinds[field] == "field"
    for button in ("New customer", "Save", "Submit", "Approve"):
        assert kinds[button] == "action"
    assert kinds["Pending approval"] == "status value"
    assert kinds["Active"] == "status value"


def test_fixture_has_no_stray_terms(entries):
    # "Uploads the ID scan" names no control, and "Pending" on its own adds nothing
    # once "Pending approval" is listed.
    terms = {e.term for e in entries}
    assert "ID scan" not in terms
    assert "Pending" not in terms
    assert len(entries) == 22


def test_sorted_by_kind_then_term(entries):
    order = ["role", "screen", "field", "action", "status value"]
    kinds = [e.kind for e in entries]
    assert kinds == sorted(kinds, key=order.index)
    for kind in order:
        terms = [e.term.lower() for e in entries if e.kind == kind]
        assert terms == sorted(terms)


def test_used_in_for_a_field(entries):
    postcode = by_term(entries)["Postcode"]
    assert postcode.used_in == ["R001", "AC001"]
    assert postcode.where == "Customer search screen"
    assert postcode.first_seen == 8.0
    assert postcode.keyframe_index == 0


def test_used_in_for_a_role(entries):
    manager = by_term(entries)["Team manager"]
    assert manager.used_in == ["S04", "R004", "AC007", "Q004"]
    assert manager.where == "listed as an actor"
    # First named at journey step 5, on frame 5.
    assert manager.first_seen == 240.0
    assert manager.keyframe_index == 5


def test_used_in_for_a_button_is_an_exact_match(entries):
    # "approves" in S04 and R004 does not count; the whole word "approve" in Q004 does.
    assert by_term(entries)["Approve"].used_in == ["A005", "AC007", "Q004"]


def test_status_value_from_a_criterion(entries):
    active = by_term(entries)["Active"]
    assert active.where == "in AC007"
    assert active.used_in == ["R004", "AC007"]


def test_definition_and_notes_start_empty(entries):
    assert all(e.definition == "" for e in entries)
    assert all(e.notes == "" for e in entries)


def test_fixture_is_consistent(analysis, entries):
    assert consistency_findings(entries, analysis) == []


# ------------------------------------------------------------------ small helpers


@pytest.mark.parametrize("description, expected", [
    ("Presses Save and continue", "Save and continue"),
    ("Presses New customer", "New customer"),
    ('Clicks the "Approve" button', "Approve"),
    ("Selects Approve from the Actions menu.", "Approve"),
    ("Opens the Reports tab", "Reports"),
    ("Presses Save to store the record", "Save"),
    ("Uploads the ID scan", None),
    ("Opens the approvals queue", None),
    ("Types the postcode", None),
])
def test_control_name(description, expected):
    assert control_name(description) == expected


@pytest.mark.parametrize("statement, expected", [
    ("As an onboarding clerk, I need to attach a document, so that it is on file.", "onboarding clerk"),
    ("As a Team leader, I need to approve customers.", "Team leader"),
    ("The system must reject the record.", None),
])
def test_role_in_statement(statement, expected):
    assert role_in_statement(statement) == expected


# ------------------------------------------------------------------ the naming check


def _field(id_: str, screen_id: str, label: str, ts: float, kf: int) -> DataField:
    return DataField(id=id_, screen_id=screen_id, label=label, field_type="text", source="seen on screen",
                     timestamp=ts, keyframe_index=kf)


@pytest.fixture
def inconsistent() -> Analysis:
    return Analysis(
        title="Naming test",
        summary="Two spellings of one field and a role that is not an actor.",
        actors=["Team lead"],
        screens=[
            Screen(id="S01", name="Customer search", purpose="Find a customer.", keyframe_indexes=[0],
                   first_seen=0.0, field_ids=["F001"]),
            Screen(id="S02", name="Customer details", purpose="Enter the details.", keyframe_indexes=[1],
                   first_seen=30.0, field_ids=["F002"]),
        ],
        fields=[
            _field("F001", "S01", "Postcode", 5.0, 0),
            _field("F002", "S02", "Post code", 35.0, 1),
        ],
        requirements=[
            Requirement(id="R001", statement="As a team leader, I need to approve a customer, so that they become active.",
                        source_quote="The team leader signs it off.", timestamp=40.0, keyframe_index=1,
                        confidence="high"),
        ],
        acceptance_criteria=[
            AcceptanceCriterion(id="AC001", requirement_id="R001", given="a customer is waiting",
                                when="the team leader presses Approve", then="the customer is Active",
                                timestamp=41.0, keyframe_index=1),
        ],
    )


def test_findings_on_inconsistent_naming(inconsistent):
    entries, findings = glossary_with_findings(inconsistent)
    assert len(findings) == 3, findings

    near_dup = [f for f in findings if "Postcode" in f and "Post code" in f and not f.startswith("Info:")]
    assert len(near_dup) == 1
    assert "space or hyphen" in near_dup[0]

    info = [f for f in findings if f.startswith("Info:")]
    assert len(info) == 1
    assert "2 screens" in info[0]
    assert "Customer search" in info[0] and "Customer details" in info[0]

    role = [f for f in findings if "team leader" in f and "R001" in f]
    assert len(role) == 1
    assert "not in the actors list" in role[0]
    assert 'closest listed actor is "Team lead"' in role[0]


def test_findings_are_written_into_notes(inconsistent):
    entries, findings = glossary_with_findings(inconsistent)
    terms = by_term(entries)
    assert "Post code" in terms["Postcode"].notes and "space or hyphen" in terms["Postcode"].notes
    assert "Postcode" in terms["Post code"].notes
    assert "Info:" in terms["Postcode"].notes and "Info:" in terms["Post code"].notes
    assert "not in the actors list" in terms["team leader"].notes
    assert terms["Team lead"].notes == ""
    # Running the check twice does not double the notes.
    before = {e.term: e.notes for e in entries}
    consistency_findings(entries, inconsistent)
    assert {e.term: e.notes for e in entries} == before


def test_roles_from_statements_get_an_entry(inconsistent):
    terms = by_term(build_glossary(inconsistent))
    leader = terms["team leader"]
    assert leader.kind == "role"
    assert leader.where == "in requirement R001"
    assert leader.used_in == ["R001", "AC001"]
    assert terms["Team lead"].used_in == []


@pytest.mark.parametrize("a, b, phrase", [
    ("Customer Detail", "Customer Details", "plural"),
    ("customer details", "Customer Details", "capital letters"),
    ("Post-code", "Postcode", "space or hyphen"),
    ("Team lead", "Team leader", "one or two letters apart"),
])
def test_near_duplicate_rules(a, b, phrase):
    analysis = Analysis(title="t", summary="s", screens=[
        Screen(id="S01", name=a, purpose="", keyframe_indexes=[0], first_seen=0.0),
        Screen(id="S02", name=b, purpose="", keyframe_indexes=[1], first_seen=1.0),
    ])
    findings = consistency_findings(build_glossary(analysis), analysis)
    assert len(findings) == 1
    assert phrase in findings[0]


def test_short_or_unrelated_terms_are_not_near_duplicates():
    analysis = Analysis(title="t", summary="s", screens=[
        Screen(id="S01", name="Save", purpose="", keyframe_indexes=[0], first_seen=0.0),
        Screen(id="S02", name="Fail", purpose="", keyframe_indexes=[1], first_seen=1.0),
        Screen(id="S03", name="First name", purpose="", keyframe_indexes=[2], first_seen=2.0),
        Screen(id="S04", name="Last name", purpose="", keyframe_indexes=[3], first_seen=3.0),
    ])
    assert consistency_findings(build_glossary(analysis), analysis) == []


# ------------------------------------------------------------------ output


def test_rows_have_the_header_and_one_row_per_entry(entries):
    rows = glossary_rows(entries)
    assert rows[0] == ["Term", "Kind", "Where", "First seen", "Frame", "Used in", "Definition", "Notes"]
    assert rows[0] == GLOSSARY_HEADERS
    assert len(rows) == len(entries) + 1
    postcode = next(r for r in rows[1:] if r[0] == "Postcode")
    assert postcode == ["Postcode", "field", "Customer search screen", "00:08", 0, "R001, AC001", "", ""]
    assert all(isinstance(r[4], int) for r in rows[1:])


def test_markdown_mentions_every_term(entries):
    text = glossary_markdown(entries)
    assert text.startswith("## Glossary\n")
    assert "| Term | Kind | Where | First seen | Frame | Used in | Definition | Notes |" in text
    for e in entries:
        assert f"| {e.term} |" in text
    assert "| 00:08 | 0 | R001, AC001 |" in text


def test_markdown_escapes_pipes():
    entry = GlossaryEntry(term="A | B", kind="other", first_seen=0.0, keyframe_index=0, where="x")
    lines = glossary_markdown([entry]).splitlines()
    assert "| A \\| B | other | x | 00:00 | 0 |  |  |  |" in lines


def test_empty_analysis():
    analysis = Analysis(title="Empty", summary="Nothing yet.")
    entries, findings = glossary_with_findings(analysis)
    assert entries == [] and findings == []
    assert glossary_rows(entries) == [GLOSSARY_HEADERS]
    assert "No terms found." in glossary_markdown(entries)
