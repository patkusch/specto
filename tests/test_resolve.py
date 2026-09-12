"""Turning typed answers into requirements, with the fake model. No API, no network."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from specto.fake import FakeCaller
from specto.model import Analysis
from specto.prompts import SYSTEM_PROMPT_RESOLVE
from specto.resolve import ResolveResponse, answered_questions, resolve, resolve_dir

FIXTURES = Path(__file__).parent / "fixtures"

Q002_ANSWER = "No. The email is optional. The record saves without one."
Q003_ANSWER = "Passport or driving licence as PDF or JPEG, up to 10 MB."


def text_of(content_blocks: list[dict]) -> str:
    return "\n".join(b["text"] for b in content_blocks if b["type"] == "text")


@pytest.fixture
def analysis() -> Analysis:
    return Analysis.model_validate(json.loads((FIXTURES / "sample_analysis.json").read_text()))


@pytest.fixture
def answered(analysis) -> Analysis:
    """The fixture with Q002 and Q003 answered, Q001 open and Q004 not needed."""
    by_id = {q.id: q for q in analysis.questions}
    by_id["Q002"].answer, by_id["Q002"].status = Q002_ANSWER, "answered"
    by_id["Q003"].answer, by_id["Q003"].status = Q003_ANSWER, "answered"
    by_id["Q004"].status = "not needed"
    return analysis


def test_answered_questions_picks_only_answered_with_text(answered):
    assert [q.id for q in answered_questions(answered)] == ["Q002", "Q003"]
    assert [q.id for q in answered_questions(answered, skip_ids=["Q002"])] == ["Q003"]
    # Status answered with nothing typed does not count.
    answered.questions[2].answer = "  "
    assert [q.id for q in answered_questions(answered)] == ["Q002"]


def test_request_carries_the_answered_questions_only(answered):
    caller = FakeCaller()

    resolve(answered, caller, log=lambda _: None)

    assert len(caller.calls) == 1
    call = caller.calls[0]
    assert call["system"] == SYSTEM_PROMPT_RESOLVE
    assert call["output_model"] is ResolveResponse
    assert all(b["type"] == "text" for b in call["content_blocks"])
    text = text_of(call["content_blocks"])
    assert "Q002" in text and Q002_ANSWER in text
    assert "Q003" in text and Q003_ANSWER in text
    assert "Q001" not in text
    assert "Q004" not in text
    assert "does the clerk have to use it" not in text  # Q001's wording
    assert "when the team manager is away" not in text  # Q004's wording
    # The current analysis goes along so the model can update and link to it.
    assert '"R001"' in text and '"AC007"' in text and '"S03"' in text


def test_merge_gives_fresh_ids_and_applies_updates(answered):
    caller = FakeCaller()
    before_statement = answered.requirements[0].statement

    merged, response = resolve(answered, caller, log=lambda _: None)

    assert len(response.new_requirements) == 1
    new = merged.requirements[-1]
    assert new.id == "R005"
    assert [r.id for r in merged.requirements] == ["R001", "R002", "R003", "R004", "R005"]
    assert new.source_quote == f"Answer to Q002: {Q002_ANSWER}"
    assert new.keyframe_index == 1 and new.timestamp == 74.0  # copied from Q002
    assert new.screen_id == "S02"

    criterion = merged.acceptance_criteria[-1]
    assert criterion.id == "AC008"
    assert criterion.requirement_id == "R005"
    assert [ac.id for ac in merged.acceptance_criteria] == [f"AC{n:03d}" for n in range(1, 9)]

    first = merged.requirements[0]
    assert first.statement != before_statement
    assert "Q002" in first.statement
    assert "Q002 narrows it." in (first.rationale or "")

    follow_up = merged.questions[-1]
    assert follow_up.id == "Q005"
    assert follow_up.status == "open"
    assert follow_up.answer is None
    assert follow_up.keyframe_index == 1
    assert [q.id for q in merged.questions] == ["Q001", "Q002", "Q003", "Q004", "Q005"]

    by_id = {q.id: q for q in merged.questions}
    assert by_id["Q001"].status == "open"
    assert by_id["Q002"].status == "answered"
    assert by_id["Q003"].status == "answered"
    assert by_id["Q004"].status == "not needed"

    assert merged.usage is not None
    assert merged.usage.calls == 4  # fixture had 3
    assert merged.usage.output_tokens == 6120 + 300


def test_merge_drops_dangling_links_and_logs(answered):
    from specto.model import AcceptanceCriterion, Question, Requirement
    from specto.resolve import StatementUpdate, merge

    lines: list[str] = []
    response = ResolveResponse(
        new_requirements=[Requirement(id="R901", statement="X.", source_quote="Answer to Q002: x", timestamp=74.0,
                                      keyframe_index=1, screen_id="S99", confidence="high")],
        updated_statements=[StatementUpdate(requirement_id="R777", statement="Y.", reason="Q002")],
        new_acceptance_criteria=[
            AcceptanceCriterion(id="AC901", requirement_id="R901", given="a", when="b", then="c", timestamp=74.0, keyframe_index=1),
            AcceptanceCriterion(id="AC902", requirement_id="R888", given="a", when="b", then="c", timestamp=74.0, keyframe_index=1),
        ],
        follow_up_questions=[Question(id="Q901", question="Z?", why_it_matters="w", timestamp=74.0, keyframe_index=1,
                                      status="answered", answer="should be cleared")],
    )

    merge(answered, response, log=lines.append)

    assert answered.requirements[-1].id == "R005" and answered.requirements[-1].screen_id is None
    assert [ac.id for ac in answered.acceptance_criteria[-1:]] == ["AC008"]
    assert answered.questions[-1].status == "open" and answered.questions[-1].answer is None
    assert any("R777" in line for line in lines)
    assert any("R888" in line for line in lines)
    assert any("S99" in line for line in lines)


def test_resolve_with_nothing_answered_makes_no_call(analysis):
    caller = FakeCaller()
    merged, response = resolve(analysis, caller, log=lambda _: None)
    assert caller.calls == []
    assert response == ResolveResponse()
    assert merged == analysis


def test_resolve_dir_skips_when_no_answers(analysis, tmp_path):
    path = tmp_path / "analysis.json"
    path.write_text(analysis.model_dump_json(indent=2))
    caller = FakeCaller()
    lines: list[str] = []

    response = resolve_dir(tmp_path, caller=caller, log=lines.append)

    assert caller.calls == []
    assert response.is_empty()
    assert Analysis.model_validate_json(path.read_text()) == analysis
    assert any("nothing to resolve" in line for line in lines)
    assert not (tmp_path / "resolved_questions.json").exists()


def test_resolve_dir_writes_back_and_does_not_resend_resolved_answers(answered, tmp_path):
    path = tmp_path / "analysis.json"
    path.write_text(answered.model_dump_json(indent=2))

    first = FakeCaller()
    response = resolve_dir(tmp_path, caller=first, log=lambda _: None)
    assert len(first.calls) == 1
    assert len(response.new_requirements) == 1
    reloaded = Analysis.model_validate_json(path.read_text())
    assert [r.id for r in reloaded.requirements] == ["R001", "R002", "R003", "R004", "R005"]
    assert reloaded.questions[-1].id == "Q005"
    assert json.loads((tmp_path / "resolved_questions.json").read_text()) == ["Q002", "Q003"]

    # Same answers again: nothing new to send.
    second = FakeCaller()
    assert resolve_dir(tmp_path, caller=second, log=lambda _: None).is_empty()
    assert second.calls == []

    # A new answer arrives: only that one goes.
    reloaded.questions[0].answer, reloaded.questions[0].status = "They can still create one.", "answered"
    path.write_text(reloaded.model_dump_json(indent=2))
    third = FakeCaller()
    resolve_dir(tmp_path, caller=third, log=lambda _: None)
    blocks = third.calls[0]["content_blocks"]
    answers_block = next(b["text"] for b in blocks if b["text"].startswith("Answered questions"))
    assert "(1)" in answers_block
    assert "They can still create one." in answers_block
    assert Q002_ANSWER not in answers_block  # it now sits in R005's source quote, not here
    assert "Q003" not in answers_block
    assert json.loads((tmp_path / "resolved_questions.json").read_text()) == ["Q002", "Q003", "Q001"]
    assert [r.id for r in Analysis.model_validate_json(path.read_text()).requirements][-1] == "R006"
