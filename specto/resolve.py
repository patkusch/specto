"""Turn the analyst's typed answers into requirements.

Once `specto.answers` has put the expert's answers on the questions, this
module sends the answered questions and the current analysis to the model and
merges what comes back: new requirements, corrected statements, new acceptance
criteria and any follow-up questions. Open questions are never sent.

Every id the model gives is temporary. New items get the next free id after
the highest one already in the analysis, so existing ids in the workbook and
the ticket files stay valid.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Iterable, Optional

from pydantic import BaseModel, Field

from specto.extract import ClaudeCaller, ModelCaller, _add_usage, _log_call
from specto.model import AcceptanceCriterion, Analysis, Question, Requirement
from specto.prompts import SYSTEM_PROMPT_RESOLVE

RESOLVED_FILE = "resolved_questions.json"
ID_NUMBER = re.compile(r"(\d+)$")


# --------------------------------------------------------------- response shape


class StatementUpdate(BaseModel):
    """A change to an existing requirement's wording, with the reason."""

    requirement_id: str
    statement: str = Field(description="The full new statement")
    reason: str = Field(description="One line naming the question that changed it")


class ResolveResponse(BaseModel):
    """What the answered questions add to or change in the analysis."""

    new_requirements: list[Requirement] = []
    updated_statements: list[StatementUpdate] = []
    new_acceptance_criteria: list[AcceptanceCriterion] = []
    follow_up_questions: list[Question] = []

    def is_empty(self) -> bool:
        return not (
            self.new_requirements or self.updated_statements
            or self.new_acceptance_criteria or self.follow_up_questions
        )


# ------------------------------------------------------------------ the request


def answered_questions(analysis: Analysis, skip_ids: Iterable[str] = ()) -> list[Question]:
    """The questions with a typed answer and status answered, minus `skip_ids`."""
    skip = set(skip_ids)
    return [
        q for q in analysis.questions
        if q.status == "answered" and q.answer and q.answer.strip() and q.id not in skip
    ]


def build_resolve_content(analysis: Analysis, answered: list[Question]) -> list[dict]:
    """Text-only request: the analysis as it stands, then the answered questions."""
    current = {
        "title": analysis.title,
        "summary": analysis.summary,
        "actors": analysis.actors,
        "screens": [{"id": s.id, "name": s.name, "purpose": s.purpose} for s in analysis.screens],
        "requirements": [r.model_dump() for r in analysis.requirements],
        "acceptance_criteria": [ac.model_dump() for ac in analysis.acceptance_criteria],
    }
    answers = [q.model_dump() for q in answered]
    return [
        {"type": "text", "text": "Current analysis (JSON):\n" + json.dumps(current, indent=1)},
        {
            "type": "text",
            "text": f"Answered questions ({len(answers)}), each with the analyst's typed answer (JSON):\n"
            + json.dumps(answers, indent=1),
        },
        {
            "type": "text",
            "text": "Return what these answers add to or change in the analysis. "
            "Return nothing for an answer that changes nothing.",
        },
    ]


# ---------------------------------------------------------------------- merging


def _next_number(ids: Iterable[str]) -> int:
    highest = 0
    for item in ids:
        match = ID_NUMBER.search(item)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def merge(analysis: Analysis, response: ResolveResponse, log: Callable[[str], None] = print) -> Analysis:
    """Add the response to the analysis in place, giving new items fresh ids.

    Criteria that point at a requirement that exists in neither the analysis
    nor the response are dropped and logged. Statement updates for unknown
    requirements are logged and skipped. Follow-up questions always come back
    open, whatever the model set.
    """
    screen_ids = {s.id for s in analysis.screens}
    requirement_ids = {r.id for r in analysis.requirements}

    new_requirement_ids: dict[str, str] = {}
    number = _next_number(requirement_ids)
    for requirement in response.new_requirements:
        new_id = f"R{number:03d}"
        number += 1
        new_requirement_ids[requirement.id] = new_id
        requirement.id = new_id
        if requirement.screen_id and requirement.screen_id not in screen_ids:
            log(f"new requirement {new_id} points at screen {requirement.screen_id}, which does not exist; cleared")
            requirement.screen_id = None
        analysis.requirements.append(requirement)
        requirement_ids.add(new_id)

    by_id = {r.id: r for r in analysis.requirements}
    for update in response.updated_statements:
        target = by_id.get(new_requirement_ids.get(update.requirement_id, update.requirement_id))
        if target is None:
            log(f"statement update for {update.requirement_id}, which does not exist; skipped")
            continue
        target.statement = update.statement
        note = f"Updated after the expert's answer: {update.reason}".strip()
        target.rationale = f"{target.rationale} {note}".strip() if target.rationale else note

    number = _next_number(ac.id for ac in analysis.acceptance_criteria)
    for criterion in response.new_acceptance_criteria:
        owner = new_requirement_ids.get(criterion.requirement_id, criterion.requirement_id)
        if owner not in requirement_ids:
            log(f"dropping acceptance criterion for {criterion.requirement_id}: requirement does not exist")
            continue
        criterion.requirement_id = owner
        criterion.id = f"AC{number:03d}"
        number += 1
        analysis.acceptance_criteria.append(criterion)

    number = _next_number(q.id for q in analysis.questions)
    for question in response.follow_up_questions:
        question.id = f"Q{number:03d}"
        number += 1
        question.status = "open"
        question.answer = None
        if question.screen_id and question.screen_id not in screen_ids:
            question.screen_id = None
        analysis.questions.append(question)
    return analysis


# ------------------------------------------------------------------ entry points


def resolve(
    analysis: Analysis,
    caller: ModelCaller,
    log: Callable[[str], None] = print,
    skip_ids: Iterable[str] = (),
) -> tuple[Analysis, ResolveResponse]:
    """Send the answered questions to the model and merge the reply in.

    `skip_ids` are answered questions already turned into requirements by an
    earlier run; they are left out of the request so nothing is added twice.
    With no answered question to send, no call is made and the response is
    empty.
    """
    answered = answered_questions(analysis, skip_ids)
    if not answered:
        log("no answered questions to resolve")
        return analysis, ResolveResponse()
    content = build_resolve_content(analysis, answered)
    parsed, call_usage = caller(SYSTEM_PROMPT_RESOLVE, content, ResolveResponse)
    response = ResolveResponse.model_validate(parsed.model_dump())
    _add_usage(analysis.usage, call_usage)
    _log_call(log, f"resolve ({len(answered)} answers)", call_usage)
    merge(analysis, response, log=log)
    for question in answered:
        question.status = "answered"
    log(
        f"resolved {len(answered)} answers: {len(response.new_requirements)} new requirements, "
        f"{len(response.updated_statements)} updated, {len(response.new_acceptance_criteria)} criteria, "
        f"{len(response.follow_up_questions)} follow-up questions"
    )
    return analysis, response


def load_resolved_ids(out_dir: Path | str) -> list[str]:
    """Ids of questions an earlier run already resolved, from out_dir/resolved_questions.json."""
    path = Path(out_dir) / RESOLVED_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    return [str(i) for i in data] if isinstance(data, list) else []


def resolve_dir(
    out_dir: Path | str,
    caller: Optional[ModelCaller] = None,
    log: Callable[[str], None] = print,
) -> ResolveResponse:
    """Resolve the answers in out_dir/analysis.json and write it back.

    Skips, with a log line and an empty response, when no question is
    answered or every answered question was resolved by an earlier run.
    Otherwise it makes one model call, saves analysis.json, and records the
    resolved question ids in resolved_questions.json so a re-run after more
    answers only sends the new ones. It does not re-export the workbook.
    """
    out_dir = Path(out_dir)
    analysis_path = out_dir / "analysis.json"
    analysis = Analysis.model_validate_json(analysis_path.read_text())
    already = load_resolved_ids(out_dir)
    answered = answered_questions(analysis, already)
    if not answered:
        log("no new answers in analysis.json; nothing to resolve")
        return ResolveResponse()

    caller = caller or ClaudeCaller()
    analysis, response = resolve(analysis, caller, log=log, skip_ids=already)
    analysis_path.write_text(analysis.model_dump_json(indent=2))
    (out_dir / RESOLVED_FILE).write_text(json.dumps(already + [q.id for q in answered], indent=2))
    return response
