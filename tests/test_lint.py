"""Writing-check tests: one per rule, a clean story, and the summary line."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from specto.lint import (Finding, format_findings, lint_analysis, lint_criterion, lint_requirement,
                         summarize)
from specto.model import AcceptanceCriterion, Analysis, Requirement

FIXTURES = Path(__file__).parent / "fixtures"

CLEAN_STORY = "As a clerk, I need to save a customer, so that it reaches approvals."


def req(statement: str) -> Requirement:
    return Requirement(id="R001", statement=statement, source_quote="said so", timestamp=1.0,
                       keyframe_index=0, confidence="high")


def ac(given: str = "a clerk is on the customer screen", when: str = "the clerk presses Save",
       then: str = "the customer appears in the approvals queue") -> AcceptanceCriterion:
    return AcceptanceCriterion(id="AC001", requirement_id="R001", given=given, when=when, then=then,
                               timestamp=1.0, keyframe_index=0)


def rules(findings: list[Finding], severity: str | None = None) -> list[str]:
    return [f.rule for f in findings if severity is None or f.severity == severity]


# ----------------------------------------------------------------- requirements


def test_well_formed_story_is_clean():
    assert lint_requirement(req(CLEAN_STORY)) == []


def test_plain_system_sentence_is_clean():
    assert lint_requirement(req("The system must show the customer's postcode on the details screen.")) == []


def test_joined_clauses_or_is_a_warning():
    findings = lint_requirement(req("The system must reject the record when the email is blank or the postcode is blank."))
    assert rules(findings, "warn") == ["joined-clauses"]
    assert '"or"' in findings[0].message


@pytest.mark.parametrize("word", ["unless", "as well as", "but", "then"])
def test_other_joiners_warn(word):
    findings = lint_requirement(req(f"The system must save the record {word} the clerk is a manager."))
    assert "joined-clauses" in rules(findings, "warn")
    assert f'"{word}"' in " ".join(f.message for f in findings)


def test_joined_clauses_and_is_only_a_note():
    findings = lint_requirement(req("The system must show the date and time of the last save."))
    assert rules(findings) == ["joined-clauses"]
    assert findings[0].severity == "info"


def test_so_that_tail_is_not_checked_for_joiners():
    story = "As a clerk, I need to save a customer, so that the manager or the auditor can review it and sign it off."
    assert "joined-clauses" not in rules(lint_requirement(req(story)))


@pytest.mark.parametrize("word", ["appropriate", "adequate", "sufficient", "reasonable", "some", "several",
                                  "many", "quickly", "fast", "easy", "user friendly", "user-friendly",
                                  "efficient", "effective", "relevant"])
def test_vague_words_warn(word):
    findings = lint_requirement(req(f"The system must give {word} feedback."))
    assert rules(findings, "warn") == ["vague-word"]
    assert f'"{word}"' in findings[0].message


def test_vague_word_matches_whole_words_only():
    assert "vague-word" not in rules(lint_requirement(req("The system must email someone.")))
    assert "vague-word" not in rules(lint_requirement(req("The system must show the company's manifest.")))


@pytest.mark.parametrize("phrase", ["where possible", "if necessary", "as appropriate", "as required",
                                    "if practicable", "to the extent"])
def test_escape_clauses_warn(phrase):
    findings = lint_requirement(req(f"The system must log the change {phrase}."))
    assert "escape-clause" in rules(findings, "warn")


@pytest.mark.parametrize("phrase", ["etc.", "and so on", "including but not limited to"])
def test_open_ended_lists_warn(phrase):
    findings = lint_requirement(req(f"The system must store the name, {phrase}"))
    assert "open-ended-list" in rules(findings, "warn")


@pytest.mark.parametrize("word", ["database", "API", "table", "endpoint", "SQL", "JSON", "button colour"])
def test_implementation_words_are_notes(word):
    findings = lint_requirement(req(f"The system must write the customer to the {word}."))
    assert "implementation-word" in rules(findings, "info")
    assert "implementation-word" not in rules(findings, "warn")


def test_missing_actor_is_a_note():
    findings = lint_requirement(req("Save the customer when the clerk fills in the postcode."))
    assert rules(findings, "info") == ["missing-actor"]
    assert "missing-actor" not in rules(lint_requirement(req("New customers must wait in the approvals queue.")))
    assert "missing-actor" not in rules(lint_requirement(req("The clerk saves the customer.")))
    assert "missing-actor" not in rules(lint_requirement(req(CLEAN_STORY)))


def test_passive_voice_is_a_note():
    findings = lint_requirement(req("The customer record is saved when Save is pressed."))
    assert rules(findings) == ["passive-voice"]
    assert findings[0].severity == "info"
    assert '"is saved"' in findings[0].message
    assert "passive-voice" not in rules(lint_requirement(req("The screen is open when the clerk arrives.")))


def test_too_long_warns_over_forty_words():
    long = "The system must " + "show the customer's postcode, surname, first name, date of birth, " * 6 + "and email."
    findings = lint_requirement(req(long))
    assert "too-long" in rules(findings, "warn")
    forty = "The system must " + " ".join(["word"] * 37) + "."
    assert "too-long" not in rules(lint_requirement(req(forty)))


@pytest.mark.parametrize("word", ["always", "never", "all", "every", "100%"])
def test_absolutes_are_notes(word):
    findings = lint_requirement(req(f"The system must save {word} records within one second."))
    assert "absolute" in rules(findings, "info")


def test_empty_statement_warns():
    assert rules(lint_requirement(req("   "))) == ["empty-clause"]


# -------------------------------------------------------------------- criteria


def test_well_formed_criterion_is_clean():
    assert lint_criterion(ac()) == []


@pytest.mark.parametrize("part", ["given", "when", "then"])
def test_empty_clause_warns(part):
    findings = lint_criterion(ac(**{part: "  "}))
    assert rules(findings) == ["empty-clause"]
    assert f'"{part}"' in findings[0].message


def test_and_is_a_note_in_given_and_then_but_a_warning_in_when():
    given = lint_criterion(ac(given="a clerk is logged in and on the customer screen"))
    assert [(f.rule, f.severity) for f in given] == [("joined-clauses", "info")]
    when = lint_criterion(ac(when="the clerk fills in the postcode and presses Save"))
    assert [(f.rule, f.severity) for f in when] == [("joined-clauses", "warn")]
    then = lint_criterion(ac(then="the record is saved and an email is sent"))
    assert [(f.rule, f.severity) for f in then] == [("joined-clauses", "info")]


def test_or_in_a_criterion_warns():
    findings = lint_criterion(ac(when="the clerk presses Save or Submit"))
    assert rules(findings, "warn") == ["joined-clauses"]


@pytest.mark.parametrize("then", ["it works", "success", "correctly", "It works.", "the error message"])
def test_then_without_an_observable_outcome_warns(then):
    assert "no-outcome" in rules(lint_criterion(ac(then=then)), "warn")


@pytest.mark.parametrize("then", ["the customer details screen opens",
                                  "the customer's status changes to Active",
                                  "the record is not saved",
                                  "the Save button is disabled"])
def test_then_with_an_outcome_passes(then):
    assert "no-outcome" not in rules(lint_criterion(ac(then=then)))


def test_vague_word_in_a_criterion_names_the_part():
    findings = lint_criterion(ac(when="the clerk waits some time"))
    assert rules(findings, "warn") == ["vague-word"]
    assert 'in the "when" part' in findings[0].message


# ------------------------------------------------------------ whole analysis


def test_lint_analysis_keys_by_item_id():
    analysis = Analysis.model_validate(json.loads((FIXTURES / "sample_analysis.json").read_text()))
    checks = lint_analysis(analysis)
    assert set(checks) == {r.id for r in analysis.requirements} | {a.id for a in analysis.acceptance_criteria}
    assert rules(checks["R002"], "warn") == ["joined-clauses"]
    assert checks["R003"] == []


def test_summarize_counts_warnings_and_notes():
    warn = Finding(rule="vague-word", message="x", severity="warn")
    note = Finding(rule="absolute", message="y", severity="info")
    text = summarize({"R001": [warn, note], "R002": [note], "R003": [warn], "AC001": [warn, warn], "AC002": []})
    assert text == "2 requirements and 1 criterion have warnings; 2 notes"
    assert summarize({"R001": [], "AC001": [note]}) == "0 requirements and 0 criteria have warnings; 1 note"


def test_format_findings_joins_with_semicolons():
    warn = Finding(rule="vague-word", message="x is vague.", severity="warn")
    note = Finding(rule="absolute", message="y is an absolute.", severity="info")
    assert format_findings([warn, note]) == "warn: x is vague.; info: y is an absolute."
    assert format_findings([]) == ""
