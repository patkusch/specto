"""What the analysis does not yet cover, so the next conversation with the
expert is aimed at the holes.

Nothing here calls a model. Each check looks across the analysis for something
that should be there and is not: a screen no requirement talks about, a field
no requirement or criterion names, a requirement with no way to test it, a
button that goes somewhere we never saw, a journey step no requirement backs.
Each finding is one plain sentence that says what is missing and what to ask.

Two levels: "warn" is a hole to close before the requirements are handed on;
"info" is a note the analyst should know about but may decide to leave.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

from specto.model import Analysis, Requirement, Screen
from specto.timefmt import mmss

Kind = Literal[
    "screen without requirements",
    "screen without fields",
    "field never mentioned",
    "requirement without criteria",
    "requirement without screen",
    "action leading nowhere",
    "journey step not in a requirement",
    "low-confidence requirement",
    "question with no screen",
    "actor never named in a requirement",
]
Severity = Literal["warn", "info"]

# The order kinds appear in on the sheet, in the summary and in the report:
# where the system is (screens), what it holds (fields), what it must do
# (requirements), how you move through it (actions, journey), then the notes.
KIND_ORDER: tuple[str, ...] = (
    "screen without requirements",
    "screen without fields",
    "field never mentioned",
    "requirement without criteria",
    "requirement without screen",
    "action leading nowhere",
    "journey step not in a requirement",
    "low-confidence requirement",
    "question with no screen",
    "actor never named in a requirement",
)

# How each kind reads when there is more than one of it, for the summary line.
PLURALS: dict[str, str] = {
    "screen without requirements": "screens without requirements",
    "screen without fields": "screens without fields",
    "field never mentioned": "fields never mentioned",
    "requirement without criteria": "requirements without criteria",
    "requirement without screen": "requirements without screen",
    "action leading nowhere": "actions leading nowhere",
    "journey step not in a requirement": "journey steps not in a requirement",
    "low-confidence requirement": "low-confidence requirements",
    "question with no screen": "questions with no screen",
    "actor never named in a requirement": "actors never named in a requirement",
}

GAPS_HEADERS = ["What is missing", "Item", "Name", "Detail", "Time", "Frame", "Severity"]

# A requirement of one of these kinds can reasonably apply to the whole system
# rather than one screen, so a missing screen is a note, not a hole.
KINDS_WITHOUT_A_SCREEN = ("workflow", "non-functional", "reporting")

# An action description that starts like one of these is a move to somewhere:
# pressing a button, opening a menu, going to or switching to another screen or
# tab. "Selects" on its own is left out, since picking a dropdown value goes nowhere.
NAVIGATION = re.compile(
    r"\b(?:press(?:es)?|click(?:s)?|tap(?:s)?|open(?:s)?|go(?:es)? to|navigat(?:es)? to|"
    r"switch(?:es)? to|return(?:s)? to|go(?:es)? back|"
    r"select(?:s)?\s+(?:the\s+|a\s+)?(?:[\w-]+\s+){0,3}tab)\b",
    re.IGNORECASE,
)

_SEVERITY_RANK = {"warn": 0, "info": 1}


class Gap(BaseModel):
    kind: Kind
    item_id: str = Field(description="The id of the row the gap is about: S02, F004, R003, A005, J3, Q002, or the actor's name")
    item_name: str = Field(description="A plain label for the item: the screen name, field label, requirement statement ...")
    detail: str = Field(description="One sentence: what is missing and what to ask the expert")
    timestamp: float = Field(description="Seconds from the start of the recording")
    keyframe_index: int
    severity: Severity = "warn"


# ------------------------------------------------------------------ matching


_PATTERNS: dict[str, re.Pattern] = {}


def _pattern(term: str) -> re.Pattern:
    """Whole-word, case-insensitive match of a term, any whitespace or hyphen
    between its words, and an optional plural on the last word."""
    if term not in _PATTERNS:
        parts = [re.escape(p) for p in re.split(r"[\s-]+", term.strip()) if p]
        body = r"[\s-]+".join(parts) + r"(?:s|es)?"
        _PATTERNS[term] = re.compile(r"(?<![\w-])" + body + r"(?![\w-])", re.IGNORECASE)
    return _PATTERNS[term]


def mentions(term: str, text: Optional[str]) -> bool:
    """True when the term appears in the text as a whole word, ignoring case,
    with a plural allowed: "Email addresses" mentions "Email address"."""
    if not term.strip() or not text:
        return False
    return _pattern(term).search(text) is not None


def _natural(item_id: str) -> tuple:
    """Sort key so R2 comes before R10."""
    return tuple(int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", item_id))


# ------------------------------------------------------------------ lookups


def _screen_names(analysis: Analysis) -> dict[str, str]:
    return {s.id: s.name for s in analysis.screens}


def _screen_name(analysis: Analysis, screen_id: Optional[str]) -> str:
    if not screen_id:
        return ""
    return _screen_names(analysis).get(screen_id, screen_id)


def _requirement_texts(analysis: Analysis) -> list[str]:
    """Every statement and rationale, the places a field or screen ought to be named."""
    texts = []
    for r in analysis.requirements:
        texts.append(r.statement)
        if r.rationale:
            texts.append(r.rationale)
    return texts


def _criterion_texts(analysis: Analysis) -> list[str]:
    return [" ".join((ac.given, ac.when, ac.then)) for ac in analysis.acceptance_criteria]


def _screens_with_requirements(analysis: Analysis) -> set[str]:
    """Ids of the screens some requirement covers: pointed at by screen_id, or
    named in a requirement statement."""
    covered = {r.screen_id for r in analysis.requirements if r.screen_id}
    for s in analysis.screens:
        if s.id in covered:
            continue
        if any(mentions(s.name, r.statement) for r in analysis.requirements):
            covered.add(s.id)
    return covered


def _first_frame(analysis: Analysis) -> tuple[float, int]:
    if analysis.screens:
        s = min(analysis.screens, key=lambda s: s.first_seen)
        return s.first_seen, (s.keyframe_indexes[0] if s.keyframe_indexes else 0)
    return 0.0, 0


def _screen_frame(s: Screen) -> int:
    return s.keyframe_indexes[0] if s.keyframe_indexes else 0


# ------------------------------------------------------------------ one check per kind


def screens_without_requirements(analysis: Analysis) -> list[Gap]:
    """A screen no requirement points at or names."""
    covered = _screens_with_requirements(analysis)
    return [
        Gap(kind="screen without requirements", item_id=s.id, item_name=s.name,
            detail=f'No requirement points at the "{s.name}" screen; ask the expert what the system must do here.',
            timestamp=s.first_seen, keyframe_index=_screen_frame(s), severity="warn")
        for s in analysis.screens if s.id not in covered
    ]


def screens_without_fields(analysis: Analysis) -> list[Gap]:
    """A screen with no data field recorded on it, by the fields list or the screen's own field_ids."""
    with_fields = {f.screen_id for f in analysis.fields} | {s.id for s in analysis.screens if s.field_ids}
    return [
        Gap(kind="screen without fields", item_id=s.id, item_name=s.name,
            detail=f'No data field was recorded on the "{s.name}" screen; ask what is typed or shown here.',
            timestamp=s.first_seen, keyframe_index=_screen_frame(s), severity="warn")
        for s in analysis.screens if s.id not in with_fields
    ]


def fields_never_mentioned(analysis: Analysis) -> list[Gap]:
    """A field whose label appears in no requirement statement or rationale and no criterion."""
    texts = _requirement_texts(analysis) + _criterion_texts(analysis)
    gaps = []
    for f in analysis.fields:
        if any(mentions(f.label, t) for t in texts):
            continue
        screen = _screen_name(analysis, f.screen_id)
        gaps.append(Gap(
            kind="field never mentioned", item_id=f.id, item_name=f.label,
            detail=(f'The field "{f.label}" on the {screen} screen is not named in any requirement or criterion; '
                    "ask whether it matters and what rule applies to it."),
            timestamp=f.timestamp, keyframe_index=f.keyframe_index, severity="warn"))
    return gaps


def requirements_without_criteria(analysis: Analysis) -> list[Gap]:
    """A requirement with no acceptance criterion, so nobody can say when it is done."""
    with_criteria = {ac.requirement_id for ac in analysis.acceptance_criteria}
    return [
        Gap(kind="requirement without criteria", item_id=r.id, item_name=r.statement,
            detail=f"{r.id} has no acceptance criteria; ask how the expert would know this is done right.",
            timestamp=r.timestamp, keyframe_index=r.keyframe_index, severity="warn")
        for r in analysis.requirements if r.id not in with_criteria
    ]


def _screen_severity(r: Requirement) -> Severity:
    return "info" if r.kind in KINDS_WITHOUT_A_SCREEN else "warn"


def requirements_without_screen(analysis: Analysis) -> list[Gap]:
    """A requirement not tied to a screen. A hole for functional, data and
    validation requirements; a note for workflow, non-functional and reporting
    ones, which can apply to the whole system."""
    return [
        Gap(kind="requirement without screen", item_id=r.id, item_name=r.statement,
            detail=f"{r.id} is not tied to a screen; ask where in the system it applies.",
            timestamp=r.timestamp, keyframe_index=r.keyframe_index, severity=_screen_severity(r))
        for r in analysis.requirements if not r.screen_id
    ]


def actions_leading_nowhere(analysis: Analysis) -> list[Gap]:
    """An action that reads like a move somewhere (presses, opens, goes to, selects a
    tab) but has no next screen recorded."""
    gaps = []
    for a in analysis.actions:
        if a.leads_to_screen_id or not NAVIGATION.search(a.description):
            continue
        screen = _screen_name(analysis, a.screen_id)
        gaps.append(Gap(
            kind="action leading nowhere", item_id=a.id, item_name=a.description,
            detail=f'"{a.description}" on the {screen} screen has no next screen recorded; ask what appears after it.',
            timestamp=a.timestamp, keyframe_index=a.keyframe_index, severity="warn"))
    return gaps


def journey_steps_not_in_a_requirement(analysis: Analysis) -> list[Gap]:
    """A journey step on a screen no requirement points at or names."""
    covered = _screens_with_requirements(analysis)
    gaps = []
    for j in analysis.journey:
        if j.screen_id in covered:
            continue
        screen = _screen_name(analysis, j.screen_id)
        gaps.append(Gap(
            kind="journey step not in a requirement", item_id=f"J{j.order}",
            item_name=f"Step {j.order}: {screen}",
            detail=(f'Step {j.order} on the {screen} screen ("{j.description}") has no requirement behind it; '
                    "ask what the system must do at this step."),
            timestamp=j.timestamp, keyframe_index=j.keyframe_index, severity="warn"))
    return gaps


def low_confidence_requirements(analysis: Analysis) -> list[Gap]:
    """A requirement the model inferred from the screen rather than heard from the expert."""
    return [
        Gap(kind="low-confidence requirement", item_id=r.id, item_name=r.statement,
            detail=f"{r.id} was inferred from the screen rather than said by the expert; confirm it with them.",
            timestamp=r.timestamp, keyframe_index=r.keyframe_index, severity="info")
        for r in analysis.requirements if r.confidence == "low"
    ]


def questions_with_no_screen(analysis: Analysis) -> list[Gap]:
    """A question not tied to a screen, so it is harder to point at when asking it."""
    return [
        Gap(kind="question with no screen", item_id=q.id, item_name=q.question,
            detail=f"{q.id} is not tied to a screen; note where it came up before asking it.",
            timestamp=q.timestamp, keyframe_index=q.keyframe_index, severity="info")
        for q in analysis.questions if not q.screen_id
    ]


def _actor_first_seen(actor: str, analysis: Analysis) -> tuple[float, int]:
    """The earliest journey step by that actor, else the first screen."""
    steps = [(j.timestamp, j.keyframe_index) for j in analysis.journey
             if j.actor and j.actor.strip().lower() == actor.strip().lower()]
    return min(steps) if steps else _first_frame(analysis)


def actors_never_named(analysis: Analysis) -> list[Gap]:
    """An actor from the actors list that no requirement statement names."""
    gaps = []
    for actor in analysis.actors:
        if any(mentions(actor, r.statement) for r in analysis.requirements):
            continue
        ts, kf = _actor_first_seen(actor, analysis)
        gaps.append(Gap(
            kind="actor never named in a requirement", item_id=actor, item_name=actor,
            detail=f'"{actor}" is listed as an actor but no requirement names them; ask what they need from the system.',
            timestamp=ts, keyframe_index=kf, severity="info"))
    return gaps


# ------------------------------------------------------------------ all together


CHECKS = (
    screens_without_requirements,
    screens_without_fields,
    fields_never_mentioned,
    requirements_without_criteria,
    requirements_without_screen,
    actions_leading_nowhere,
    journey_steps_not_in_a_requirement,
    low_confidence_requirements,
    questions_with_no_screen,
    actors_never_named,
)


def _sort_key(g: Gap) -> tuple:
    return (_SEVERITY_RANK[g.severity], KIND_ORDER.index(g.kind), _natural(g.item_id))


def find_gaps(analysis: Analysis) -> list[Gap]:
    """Run every check and return the gaps, holes first, then by kind, then by item id."""
    gaps: list[Gap] = []
    for check in CHECKS:
        gaps += check(analysis)
    return sorted(gaps, key=_sort_key)


# ------------------------------------------------------------------ output


def gaps_rows(gaps: list[Gap]) -> list[list]:
    """Header row plus one row per gap, ready for a 'Gaps' sheet.

    'Time' is mm:ss and 'Frame' is the keyframe index; the exporter turns the
    index into a link to the still image.
    """
    rows: list[list] = [list(GAPS_HEADERS)]
    for g in gaps:
        rows.append([g.kind, g.item_id, g.item_name, g.detail, mmss(g.timestamp), g.keyframe_index, g.severity])
    return rows


def _count_by_kind(gaps: list[Gap]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for g in gaps:
        counts[g.kind] = counts.get(g.kind, 0) + 1
    return [(kind, counts[kind]) for kind in KIND_ORDER if kind in counts]


def _phrase(kind: str, n: int) -> str:
    return f"{n} {kind if n == 1 else PLURALS[kind]}"


def gaps_summary(gaps: list[Gap]) -> str:
    """One line for the Summary sheet: how many holes there are, of what kind, and how many notes."""
    holes = [g for g in gaps if g.severity == "warn"]
    notes = [g for g in gaps if g.severity == "info"]
    if not holes and not notes:
        return "No gaps found."
    if holes:
        head = f"{len(holes)} gap{'' if len(holes) == 1 else 's'} to close: "
        head += ", ".join(_phrase(kind, n) for kind, n in _count_by_kind(holes))
    else:
        head = "No gaps to close"
    if notes:
        head += f"; {len(notes)} note{'' if len(notes) == 1 else 's'}"
    return head + "."


def gaps_markdown(gaps: list[Gap]) -> str:
    """A '## Gaps' section: what it is for, then a bulleted list grouped by kind."""
    lines = ["## Gaps", "",
             "What the analysis does not yet cover, so the next conversation with the expert "
             "can be aimed at the holes.", ""]
    if not gaps:
        lines += ["No gaps found.", ""]
        return "\n".join(lines)
    lines += [gaps_summary(gaps), ""]
    for kind, n in _count_by_kind(gaps):
        group = [g for g in gaps if g.kind == kind]
        label = (kind if n == 1 else PLURALS[kind]).capitalize()
        tag = "" if group[0].severity == "warn" else " (note)"
        lines += [f"**{label}**{tag}", ""]
        for g in group:
            lines.append(f"- {g.item_id} {g.item_name}: {g.detail}")
        lines.append("")
    return "\n".join(lines)
