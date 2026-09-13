"""The Gaps sheet: what the analysis does not yet cover, so the next conversation
with the expert is aimed at the holes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from specto.gaps import (
    GAPS_HEADERS,
    KIND_ORDER,
    Gap,
    actions_leading_nowhere,
    actors_never_named,
    fields_never_mentioned,
    find_gaps,
    gaps_markdown,
    gaps_rows,
    gaps_summary,
    journey_steps_not_in_a_requirement,
    low_confidence_requirements,
    mentions,
    questions_with_no_screen,
    requirements_without_criteria,
    requirements_without_screen,
    screens_without_fields,
    screens_without_requirements,
)
from specto.model import (
    AcceptanceCriterion,
    Action,
    Analysis,
    DataField,
    JourneyStep,
    Question,
    Requirement,
    Screen,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def analysis() -> Analysis:
    return Analysis.model_validate(json.loads((FIXTURES / "sample_analysis.json").read_text()))


# ------------------------------------------------------------------ builders


def screen(id: str = "S01", name: str = "Customer search", field_ids: list[str] | None = None) -> Screen:
    return Screen(id=id, name=name, purpose=f"The {name} screen.", keyframe_indexes=[0], first_seen=5.0,
                  field_ids=field_ids or [])


def field(id: str = "F001", label: str = "Postcode", screen_id: str = "S01") -> DataField:
    return DataField(id=id, screen_id=screen_id, label=label, field_type="text", source="both",
                     timestamp=8.0, keyframe_index=0)


def requirement(id: str = "R001", statement: str = "The system must search by postcode.",
                screen_id: str | None = "S01", confidence: str = "high", kind: str = "functional",
                rationale: str | None = None) -> Requirement:
    return Requirement(id=id, statement=statement, rationale=rationale, source_quote="we search first",
                       timestamp=15.0, keyframe_index=0, screen_id=screen_id, confidence=confidence, kind=kind)


def criterion(requirement_id: str = "R001", then: str = "matches are listed") -> AcceptanceCriterion:
    return AcceptanceCriterion(id=f"AC-{requirement_id}", requirement_id=requirement_id, given="a clerk is searching",
                               when="they press Search", then=then, timestamp=16.0, keyframe_index=0)


def action(id: str = "A001", description: str = "Presses Save", leads_to: str | None = None) -> Action:
    return Action(id=id, screen_id="S01", description=description, control="button", leads_to_screen_id=leads_to,
                  timestamp=30.0, keyframe_index=0)


def step(order: int = 1, screen_id: str = "S01", actor: str | None = "Onboarding clerk") -> JourneyStep:
    return JourneyStep(order=order, screen_id=screen_id, description="The clerk searches.", actor=actor,
                       timestamp=10.0, keyframe_index=0)


def question(id: str = "Q001", screen_id: str | None = None) -> Question:
    return Question(id=id, question="Is the postcode mandatory?", why_it_matters="Decides validation.",
                    timestamp=20.0, keyframe_index=0, screen_id=screen_id)


def build(**parts) -> Analysis:
    return Analysis(title="Test", summary="A test analysis.", **parts)


def kinds(gaps: list[Gap]) -> list[str]:
    return [g.kind for g in gaps]


# ------------------------------------------------------------------ the fixture


def test_fixture_gaps(analysis):
    gaps = find_gaps(analysis)
    assert [(g.kind, g.item_id) for g in gaps] == [
        ("field never mentioned", "F003"),
        ("field never mentioned", "F004"),
        ("field never mentioned", "F005"),
        ("field never mentioned", "F009"),
        ("action leading nowhere", "A005"),
    ]
    assert all(g.severity == "warn" for g in gaps)


def test_fixture_details_say_what_to_ask(analysis):
    by_id = {g.item_id: g for g in find_gaps(analysis)}
    assert by_id["F009"].item_name == "Verification result"
    assert "Identity check screen" in by_id["F009"].detail
    assert by_id["F009"].detail.endswith("ask whether it matters and what rule applies to it.")
    assert by_id["A005"].detail == ('"Presses Approve" on the Approvals queue screen has no next screen recorded; '
                                    "ask what appears after it.")
    assert (by_id["A005"].timestamp, by_id["A005"].keyframe_index) == (265.0, 5)


def test_fixture_summary(analysis):
    assert gaps_summary(find_gaps(analysis)) == "5 gaps to close: 4 fields never mentioned, 1 action leading nowhere."


# ------------------------------------------------------------------ mentions


@pytest.mark.parametrize("term, text, expected", [
    ("Email address", "The Email addresses are checked", True),
    ("Email address", "the email address is required", True),
    ("Email address", "the e-mail address", False),
    ("Post code", "Search by post-code", True),
    ("Title", "the customer is entitled to a refund", False),
    ("Title", "Titles are optional", True),
    ("Status", "the status changes", True),
    ("Status", "", False),
    ("Status", None, False),
    ("", "anything", False),
])
def test_mentions(term, text, expected):
    assert mentions(term, text) is expected


# ------------------------------------------------------------------ one check per kind


def test_screen_without_requirements():
    a = build(screens=[screen("S01", "Customer search"), screen("S02", "Customer details")],
              requirements=[requirement(screen_id="S01")])
    gaps = screens_without_requirements(a)
    assert [(g.item_id, g.item_name, g.severity) for g in gaps] == [("S02", "Customer details", "warn")]
    assert gaps[0].detail == 'No requirement points at the "Customer details" screen; ask the expert what the system must do here.'


def test_screen_named_in_a_statement_counts_as_covered():
    a = build(screens=[screen("S02", "Approvals queue")],
              requirements=[requirement(statement="New customers wait in the approvals queue.", screen_id=None)])
    assert screens_without_requirements(a) == []


def test_screen_without_fields():
    a = build(screens=[screen("S01"), screen("S02", "Done")], fields=[field(screen_id="S01")])
    gaps = screens_without_fields(a)
    assert [(g.item_id, g.severity) for g in gaps] == [("S02", "warn")]
    assert "ask what is typed or shown here" in gaps[0].detail


def test_screen_with_field_ids_but_no_field_rows_is_not_a_gap():
    a = build(screens=[screen("S01", field_ids=["F001"])])
    assert screens_without_fields(a) == []


def test_field_never_mentioned():
    a = build(screens=[screen()], fields=[field("F001", "Postcode"), field("F002", "Surname"), field("F003", "Title")],
              requirements=[requirement(statement="The system must search by postcode.", rationale="Surnames clash.")],
              acceptance_criteria=[criterion(then="the title is shown")])
    assert fields_never_mentioned(a) == []


def test_field_never_mentioned_fires_when_only_the_quote_names_it():
    a = build(screens=[screen()], fields=[field("F001", "Postcode")],
              requirements=[Requirement(id="R001", statement="The system must search.", source_quote="by postcode",
                                        timestamp=1.0, keyframe_index=0, screen_id="S01", confidence="high")])
    gaps = fields_never_mentioned(a)
    assert [(g.item_id, g.item_name, g.severity) for g in gaps] == [("F001", "Postcode", "warn")]
    assert gaps[0].detail.startswith('The field "Postcode" on the Customer search screen is not named')


def test_field_mention_is_plural_tolerant():
    a = build(screens=[screen()], fields=[field("F001", "Email address")],
              requirements=[requirement(statement="Email addresses must have an @ sign.")])
    assert fields_never_mentioned(a) == []


def test_requirement_without_criteria():
    a = build(requirements=[requirement("R001"), requirement("R002", "The system must save.")],
              acceptance_criteria=[criterion("R001")])
    gaps = requirements_without_criteria(a)
    assert [(g.item_id, g.severity) for g in gaps] == [("R002", "warn")]
    assert gaps[0].item_name == "The system must save."
    assert gaps[0].detail == "R002 has no acceptance criteria; ask how the expert would know this is done right."
    assert requirements_without_criteria(build(requirements=[requirement()], acceptance_criteria=[criterion()])) == []


def test_requirement_without_screen():
    a = build(requirements=[requirement("R001", screen_id="S01"), requirement("R002", screen_id=None),
                            requirement("R003", screen_id=None, kind="non-functional")])
    gaps = requirements_without_screen(a)
    assert [(g.item_id, g.severity) for g in gaps] == [("R002", "warn"), ("R003", "info")]
    assert gaps[0].detail == "R002 is not tied to a screen; ask where in the system it applies."


@pytest.mark.parametrize("description", [
    "Presses Approve", "Clicks the Save button", "Opens the menu", "Goes to the queue",
    "Selects the History tab", "Switches to the Documents tab", "Taps Next",
])
def test_action_leading_nowhere_fires_on_navigation_words(description):
    a = build(screens=[screen()], actions=[action(description=description)])
    gaps = actions_leading_nowhere(a)
    assert [(g.item_id, g.item_name, g.severity) for g in gaps] == [("A001", description, "warn")]
    assert "has no next screen recorded" in gaps[0].detail


@pytest.mark.parametrize("description", ["Uploads the ID scan", "Types the postcode", "Selects Ms from the list"])
def test_action_leading_nowhere_stays_quiet_when_nothing_moves(description):
    a = build(screens=[screen()], actions=[action(description=description)])
    assert actions_leading_nowhere(a) == []


def test_action_with_a_next_screen_is_not_a_gap():
    a = build(screens=[screen()], actions=[action(description="Presses Save", leads_to="S02")])
    assert actions_leading_nowhere(a) == []


def test_journey_step_not_in_a_requirement():
    a = build(screens=[screen("S01", "Customer search"), screen("S02", "Customer details")],
              journey=[step(1, "S01"), step(2, "S02")], requirements=[requirement(screen_id="S01")])
    gaps = journey_steps_not_in_a_requirement(a)
    assert [(g.item_id, g.item_name, g.severity) for g in gaps] == [("J2", "Step 2: Customer details", "warn")]
    assert gaps[0].detail.startswith('Step 2 on the Customer details screen ("The clerk searches.") has no requirement behind it')


def test_journey_step_covered_by_a_statement_naming_the_screen():
    a = build(screens=[screen("S02", "Customer details")], journey=[step(1, "S02")],
              requirements=[requirement(statement="The Customer Details screen must show the postcode.", screen_id=None)])
    assert journey_steps_not_in_a_requirement(a) == []


def test_low_confidence_requirement():
    a = build(requirements=[requirement("R001", confidence="high"), requirement("R002", confidence="medium"),
                            requirement("R003", confidence="low")])
    gaps = low_confidence_requirements(a)
    assert [(g.item_id, g.severity) for g in gaps] == [("R003", "info")]
    assert gaps[0].detail == "R003 was inferred from the screen rather than said by the expert; confirm it with them."


def test_question_with_no_screen():
    a = build(questions=[question("Q001", screen_id="S01"), question("Q002", screen_id=None)])
    gaps = questions_with_no_screen(a)
    assert [(g.item_id, g.severity) for g in gaps] == [("Q002", "info")]
    assert gaps[0].item_name == "Is the postcode mandatory?"


def test_actor_never_named():
    a = build(actors=["Onboarding clerk", "Team manager"], screens=[screen()],
              journey=[step(1, actor="Team manager")],
              requirements=[requirement(statement="As an onboarding clerk, I need to search.")])
    gaps = actors_never_named(a)
    assert [(g.item_id, g.item_name, g.severity) for g in gaps] == [("Team manager", "Team manager", "info")]
    assert gaps[0].detail == '"Team manager" is listed as an actor but no requirement names them; ask what they need from the system.'
    # Placed at the actor's first journey step.
    assert (gaps[0].timestamp, gaps[0].keyframe_index) == (10.0, 0)


def test_actor_named_in_any_case_is_not_a_gap():
    a = build(actors=["Team manager"], requirements=[requirement(statement="A Team Manager approves the customer.")])
    assert actors_never_named(a) == []


def test_actor_with_no_journey_step_is_placed_at_the_first_screen():
    a = build(actors=["Auditor"], screens=[screen()])
    gaps = actors_never_named(a)
    assert (gaps[0].timestamp, gaps[0].keyframe_index) == (5.0, 0)


# ------------------------------------------------------------------ ordering


def test_gaps_are_sorted_by_severity_then_kind_then_item_id():
    a = build(
        actors=["Auditor"],
        screens=[screen("S01", "Search"), screen("S02", "Details"), screen("S03", "Done")],
        fields=[field("F010", "Zip", "S01"), field("F002", "Town", "S01")],
        requirements=[requirement("R001", screen_id="S01", confidence="low"),
                      requirement("R002", "The system must save.", screen_id=None)],
        actions=[action("A001", "Presses Save")],
        journey=[step(1, "S03")],
        questions=[question("Q001")],
    )
    gaps = find_gaps(a)
    severities = [g.severity for g in gaps]
    assert severities == sorted(severities, key=lambda s: 0 if s == "warn" else 1)
    warn_kinds = [g.kind for g in gaps if g.severity == "warn"]
    assert warn_kinds == sorted(warn_kinds, key=KIND_ORDER.index)
    # Within a kind, by item id with numbers compared as numbers.
    assert [g.item_id for g in gaps if g.kind == "field never mentioned"] == ["F002", "F010"]
    assert [g.item_id for g in gaps if g.kind == "screen without requirements"] == ["S02", "S03"]
    assert [g.kind for g in gaps if g.severity == "info"] == [
        "low-confidence requirement", "question with no screen", "actor never named in a requirement"]


# ------------------------------------------------------------------ rows, summary, markdown


def test_rows_header_and_count(analysis):
    gaps = find_gaps(analysis)
    rows = gaps_rows(gaps)
    assert rows[0] == GAPS_HEADERS == ["What is missing", "Item", "Name", "Detail", "Time", "Frame", "Severity"]
    assert len(rows) == len(gaps) + 1
    assert all(len(r) == len(GAPS_HEADERS) for r in rows)
    assert rows[-1] == ["action leading nowhere", "A005", "Presses Approve", gaps[-1].detail, "04:25", 5, "warn"]


def test_rows_when_empty():
    assert gaps_rows([]) == [GAPS_HEADERS]


def test_summary_when_empty():
    assert gaps_summary([]) == "No gaps found."


def test_summary_counts_holes_by_kind_and_notes_separately():
    a = build(
        actors=["Auditor"],
        screens=[screen("S01", "Search"), screen("S02", "Details"), screen("S03", "Done")],
        fields=[field("F001", "Zip", "S01")],
        requirements=[requirement("R001", screen_id="S01", confidence="low"),
                      requirement("R002", "The system must save.", screen_id="S01"),
                      requirement("R003", "The system must log.", screen_id="S01")],
        questions=[question("Q001")],
    )
    # Holes: S02 and S03 have no requirement and no fields, Zip is never named,
    # none of the three requirements has criteria. Notes: R001 is low confidence,
    # Q001 has no screen, Auditor is never named.
    assert gaps_summary(find_gaps(a)) == (
        "8 gaps to close: 2 screens without requirements, 2 screens without fields, 1 field never mentioned, "
        "3 requirements without criteria; 3 notes."
    )


def test_summary_singulars_and_notes_only():
    one = [Gap(kind="requirement without criteria", item_id="R001", item_name="x", detail="d",
               timestamp=0.0, keyframe_index=0, severity="warn")]
    assert gaps_summary(one) == "1 gap to close: 1 requirement without criteria."
    note = [Gap(kind="low-confidence requirement", item_id="R001", item_name="x", detail="d",
                timestamp=0.0, keyframe_index=0, severity="info")]
    assert gaps_summary(note) == "No gaps to close; 1 note."


def test_markdown_groups_by_kind(analysis):
    text = gaps_markdown(find_gaps(analysis))
    lines = text.splitlines()
    assert lines[0] == "## Gaps"
    assert lines[2].startswith("What the analysis does not yet cover")
    assert text.index("**Fields never mentioned**") < text.index("- F003 Title:") < text.index("- F009 Verification result:")
    assert text.index("- F009 Verification result:") < text.index("**Action leading nowhere**") < text.index("- A005 Presses Approve:")
    assert text.count("**") == 4
    assert "ask what appears after it." in text


def test_markdown_marks_notes_and_handles_empty():
    note = [Gap(kind="low-confidence requirement", item_id="R001", item_name="x", detail="d",
                timestamp=0.0, keyframe_index=0, severity="info")]
    assert "**Low-confidence requirement** (note)" in gaps_markdown(note)
    empty = gaps_markdown([])
    assert empty.startswith("## Gaps")
    assert "No gaps found." in empty
