"""Stage 2: turn a Recording into an Analysis using Claude.

Two passes:

1. `read_chunks` walks the recording a few frames at a time. Each call gets the
   frame images and the words spoken over them and returns what it saw.
2. `consolidate` takes the whole transcript plus all chunk readings and merges
   them into one Analysis.

The model is reached through a `ModelCaller`. `ClaudeCaller` is the real one;
tests pass a fake, so nothing here needs a network or an API key.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Callable, Optional, Protocol

from pydantic import BaseModel

from specto.model import (
    AcceptanceCriterion,
    Action,
    Analysis,
    DataField,
    JourneyStep,
    Moment,
    Question,
    Recording,
    Requirement,
    Screen,
    Usage,
)
from specto.prompts import SYSTEM_PROMPT_CONSOLIDATE, SYSTEM_PROMPT_READ
from specto.timefmt import mmss

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 16000
LONG_TRANSCRIPT_CHARS = 150_000  # above this, consolidation is split into two calls

USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


class ExtractionError(RuntimeError):
    """The model did not give us a usable answer."""


# --------------------------------------------------------------- response shapes


class ChunkReading(BaseModel):
    """What the model saw in one chunk of frames."""

    screens: list[Screen] = []
    fields: list[DataField] = []
    actions: list[Action] = []
    journey: list[JourneyStep] = []
    requirement_candidates: list[Requirement] = []
    questions: list[Question] = []


class AnalysisResponse(BaseModel):
    """The consolidation answer: an Analysis without the usage block."""

    title: str
    summary: str
    actors: list[str] = []
    screens: list[Screen] = []
    fields: list[DataField] = []
    actions: list[Action] = []
    journey: list[JourneyStep] = []
    requirements: list[Requirement] = []
    acceptance_criteria: list[AcceptanceCriterion] = []
    questions: list[Question] = []


class RequirementsResponse(BaseModel):
    """First half of a split consolidation (very long recordings)."""

    requirements: list[Requirement] = []
    acceptance_criteria: list[AcceptanceCriterion] = []


class StructureResponse(BaseModel):
    """Second half of a split consolidation (very long recordings)."""

    title: str
    summary: str
    actors: list[str] = []
    screens: list[Screen] = []
    fields: list[DataField] = []
    actions: list[Action] = []
    journey: list[JourneyStep] = []
    questions: list[Question] = []


# --------------------------------------------------------------------- callers


class ModelCaller(Protocol):
    """Anything that can answer one structured request.

    Returns the parsed response and a dict of token counts (see USAGE_KEYS).
    """

    def __call__(
        self, system: str, content_blocks: list[dict], output_model: type[BaseModel]
    ) -> tuple[BaseModel, dict]: ...


class ClaudeCaller:
    """Calls the Claude API with structured output and a cached system prompt."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        effort: str = "high",
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        import anthropic

        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = anthropic.Anthropic()

    def __call__(
        self, system: str, content_blocks: list[dict], output_model: type[BaseModel]
    ) -> tuple[BaseModel, dict]:
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": content_blocks}],
            output_format=output_model,
            output_config={"effort": self.effort},
        )
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise ExtractionError(f"The model refused the request: {details}")
        if response.stop_reason == "max_tokens":
            raise ExtractionError(
                f"The answer was cut off at {self.max_tokens} tokens. "
                "Use fewer frames per call or raise max_tokens."
            )
        parsed = response.parsed_output
        if parsed is None:
            raise ExtractionError("The model returned no structured output.")
        usage = {key: getattr(response.usage, key, 0) or 0 for key in USAGE_KEYS}
        return parsed, usage


# --------------------------------------------------------------------- helpers


def format_time(seconds: float) -> str:
    """83.0 -> '01:23', the same mm:ss (or h:mm:ss) text the exporter prints."""
    return mmss(seconds)


def _media_type(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "image/jpeg")


def _image_block(out_dir: Path, rel_path: str) -> dict:
    file_path = out_dir / rel_path
    if not file_path.exists():
        raise FileNotFoundError(f"Frame image not found: {file_path}")
    data = base64.standard_b64encode(file_path.read_bytes()).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": _media_type(rel_path), "data": data},
    }


def _add_usage(usage: Optional[Usage], call_usage: dict) -> None:
    if usage is None:
        return
    usage.calls += 1
    for key in USAGE_KEYS:
        setattr(usage, key, getattr(usage, key) + int(call_usage.get(key, 0) or 0))


def _known_screens_block(readings: list[ChunkReading]) -> str:
    """One line per screen already identified, so ids stay stable across chunks."""
    seen: dict[str, Screen] = {}
    for reading in readings:
        for screen in reading.screens:
            seen.setdefault(screen.id, screen)
    if not seen:
        return "Screens identified so far: none yet. Start numbering at S01."
    lines = [f"- {s.id} {s.name}: {s.purpose}" for s in seen.values()]
    return "Screens identified so far (reuse these ids when the screen reappears):\n" + "\n".join(lines)


def build_chunk_content(
    recording: Recording,
    moments: list[Moment],
    out_dir: Path,
    chunk_number: int,
    chunk_count: int,
    earlier: list[ChunkReading],
) -> list[dict]:
    """The user message for one chunk: header, known screens, then frame by frame."""
    first, last = moments[0], moments[-1]
    header = (
        f"Recording: {recording.source} ({format_time(recording.duration)} long). "
        f"This is chunk {chunk_number} of {chunk_count}: frames {first.keyframe_index} to "
        f"{last.keyframe_index}, covering {format_time(first.start)} to {format_time(last.end)}. "
        "Each frame is introduced by a line 'Frame N at mm:ss', then the image, then what the "
        "expert said while it was showing."
    )
    blocks: list[dict] = [
        {"type": "text", "text": header},
        {"type": "text", "text": _known_screens_block(earlier)},
    ]
    keyframes = {k.index: k for k in recording.keyframes}
    for moment in moments:
        keyframe = keyframes[moment.keyframe_index]
        blocks.append(
            {"type": "text", "text": f"Frame {keyframe.index} at {format_time(keyframe.timestamp)}"}
        )
        blocks.append(_image_block(out_dir, keyframe.path))
        spoken = moment.text or "(nothing said)"
        blocks.append({"type": "text", "text": f"Transcript while frame {keyframe.index} was showing: {spoken}"})
    return blocks


# ---------------------------------------------------------------------- pass 1


def read_chunks(
    recording: Recording,
    out_dir: Path | str,
    caller: ModelCaller,
    frames_per_call: int = 8,
    usage: Optional[Usage] = None,
    log: Callable[[str], None] = print,
) -> list[ChunkReading]:
    """Walk the moments in chunks and ask the model what each chunk shows.

    Token counts go into `usage` when one is given. One line is logged per call.
    """
    out_dir = Path(out_dir)
    moments = recording.moments
    if not moments:
        return []
    chunks = [moments[i : i + frames_per_call] for i in range(0, len(moments), frames_per_call)]
    readings: list[ChunkReading] = []
    for number, chunk in enumerate(chunks, start=1):
        content = build_chunk_content(recording, chunk, out_dir, number, len(chunks), readings)
        parsed, call_usage = caller(SYSTEM_PROMPT_READ, content, ChunkReading)
        reading = ChunkReading.model_validate(parsed.model_dump())
        readings.append(reading)
        _add_usage(usage, call_usage)
        log(
            f"chunk {number}/{len(chunks)}: {len(chunk)} frames, "
            f"in {call_usage.get('input_tokens', 0)} out {call_usage.get('output_tokens', 0)}, "
            f"cache read {call_usage.get('cache_read_input_tokens', 0)} "
            f"write {call_usage.get('cache_creation_input_tokens', 0)}"
        )
    return readings


# ---------------------------------------------------------------------- pass 2


def format_transcript(recording: Recording) -> str:
    """The whole transcript as '[mm:ss] text' lines."""
    lines = []
    for segment in recording.segments:
        text = segment.text.strip()
        if not text:
            continue
        who = f"{segment.speaker}: " if segment.speaker else ""
        lines.append(f"[{format_time(segment.start)}] {who}{text}")
    return "\n".join(lines) if lines else "(no transcript)"


def _consolidation_blocks(recording: Recording, readings: list[ChunkReading], ask: str) -> list[dict]:
    readings_json = json.dumps([r.model_dump() for r in readings], indent=1)
    return [
        {
            "type": "text",
            "text": (
                f"Recording: {recording.source}, {format_time(recording.duration)} long, "
                f"{len(recording.keyframes)} frames, read in {len(readings)} chunks.\n\n"
                f"Full transcript:\n{format_transcript(recording)}"
            ),
        },
        {"type": "text", "text": f"Chunk notes (JSON):\n{readings_json}"},
        {"type": "text", "text": ask},
    ]


def consolidate(
    recording: Recording,
    readings: list[ChunkReading],
    caller: ModelCaller,
    usage: Optional[Usage] = None,
    log: Callable[[str], None] = print,
) -> Analysis:
    """Merge the chunk readings into one Analysis with contiguous ids."""
    transcript_length = len(format_transcript(recording))
    if transcript_length <= LONG_TRANSCRIPT_CHARS:
        ask = "Produce the complete consolidated analysis."
        parsed, call_usage = caller(SYSTEM_PROMPT_CONSOLIDATE, _consolidation_blocks(recording, readings, ask), AnalysisResponse)
        _add_usage(usage, call_usage)
        _log_call(log, "consolidate", call_usage)
        response = AnalysisResponse.model_validate(parsed.model_dump())
    else:
        ask_one = "This recording is long, so the work is split. Produce only the final requirements and their acceptance criteria."
        parsed_one, usage_one = caller(SYSTEM_PROMPT_CONSOLIDATE, _consolidation_blocks(recording, readings, ask_one), RequirementsResponse)
        _add_usage(usage, usage_one)
        _log_call(log, "consolidate 1/2 (requirements)", usage_one)
        ask_two = "This recording is long, so the work is split. Produce the title, summary, actors, screens, fields, actions, journey and questions. Requirements were done separately."
        parsed_two, usage_two = caller(SYSTEM_PROMPT_CONSOLIDATE, _consolidation_blocks(recording, readings, ask_two), StructureResponse)
        _add_usage(usage, usage_two)
        _log_call(log, "consolidate 2/2 (structure)", usage_two)
        response = AnalysisResponse(**parsed_two.model_dump(), **parsed_one.model_dump())
    analysis = Analysis(**response.model_dump())
    return renumber(analysis, log=log)


def _log_call(log: Callable[[str], None], name: str, call_usage: dict) -> None:
    log(
        f"{name}: in {call_usage.get('input_tokens', 0)} out {call_usage.get('output_tokens', 0)}, "
        f"cache read {call_usage.get('cache_read_input_tokens', 0)} "
        f"write {call_usage.get('cache_creation_input_tokens', 0)}"
    )


# ------------------------------------------------------------------ renumbering


def renumber(analysis: Analysis, log: Callable[[str], None] = print) -> Analysis:
    """Give every item a contiguous id and fix every cross-reference.

    Order is kept as the model gave it. Items that point at something that does
    not exist are handled the safest way: optional links become None, each
    screen's field and action lists are rebuilt from the fields and actions
    themselves, acceptance criteria for a requirement that does not exist are
    removed, and a required screen link that does not resolve is kept and
    logged so a person can look at it.
    """
    screen_ids = {s.id: f"S{n:02d}" for n, s in enumerate(analysis.screens, start=1)}
    field_ids = {f.id: f"F{n:03d}" for n, f in enumerate(analysis.fields, start=1)}
    action_ids = {a.id: f"A{n:03d}" for n, a in enumerate(analysis.actions, start=1)}
    requirement_ids = {r.id: f"R{n:03d}" for n, r in enumerate(analysis.requirements, start=1)}
    question_ids = {q.id: f"Q{n:03d}" for n, q in enumerate(analysis.questions, start=1)}

    def screen_ref(old: Optional[str]) -> Optional[str]:
        return screen_ids.get(old) if old else None

    def required_screen_ref(old: str, what: str) -> str:
        new = screen_ids.get(old)
        if new is None:
            log(f"{what} points at screen {old}, which does not exist; left as is")
            return old
        return new

    for screen in analysis.screens:
        screen.id = screen_ids[screen.id]
    for field in analysis.fields:
        field.id = field_ids[field.id]
        field.screen_id = required_screen_ref(field.screen_id, f"field {field.id}")
    for action in analysis.actions:
        action.id = action_ids[action.id]
        action.screen_id = required_screen_ref(action.screen_id, f"action {action.id}")
        action.leads_to_screen_id = screen_ref(action.leads_to_screen_id)
    for step in analysis.journey:
        step.screen_id = required_screen_ref(step.screen_id, f"journey step {step.order}")
    for requirement in analysis.requirements:
        requirement.id = requirement_ids[requirement.id]
        requirement.screen_id = screen_ref(requirement.screen_id)
    for question in analysis.questions:
        question.id = question_ids[question.id]
        question.screen_id = screen_ref(question.screen_id)

    kept: list[AcceptanceCriterion] = []
    for criterion in analysis.acceptance_criteria:
        new_requirement = requirement_ids.get(criterion.requirement_id)
        if new_requirement is None:
            log(f"dropping acceptance criterion {criterion.id}: requirement {criterion.requirement_id} does not exist")
            continue
        criterion.requirement_id = new_requirement
        kept.append(criterion)
    for n, criterion in enumerate(kept, start=1):
        criterion.id = f"AC{n:03d}"
    analysis.acceptance_criteria = kept

    # Rebuild each screen's field and action lists from the fields and actions
    # themselves, so the two views cannot disagree.
    for screen in analysis.screens:
        fields = [f.id for f in analysis.fields if f.screen_id == screen.id]
        actions = [a.id for a in analysis.actions if a.screen_id == screen.id]
        screen.field_ids = fields
        screen.action_ids = actions
    for n, step in enumerate(analysis.journey, start=1):
        step.order = n
    return analysis


# ------------------------------------------------------------------ entry point


def extract(
    recording: Recording,
    out_dir: Path | str,
    caller: Optional[ModelCaller] = None,
    force: bool = False,
    frames_per_call: int = 8,
    log: Callable[[str], None] = print,
) -> Analysis:
    """Run both passes and write analysis.json; reuse it on a re-run unless force.

    Also writes chunk_readings.json, which is handy when a prompt needs tuning.
    """
    out_dir = Path(out_dir)
    analysis_path = out_dir / "analysis.json"
    if analysis_path.exists() and not force:
        log(f"reusing {analysis_path}")
        return Analysis.model_validate_json(analysis_path.read_text())

    caller = caller or ClaudeCaller()
    usage = Usage(model=getattr(caller, "model", "unknown"))
    readings = read_chunks(recording, out_dir, caller, frames_per_call=frames_per_call, usage=usage, log=log)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "chunk_readings.json").write_text(
        json.dumps([r.model_dump() for r in readings], indent=2)
    )
    analysis = consolidate(recording, readings, caller, usage=usage, log=log)
    analysis.usage = usage
    analysis_path.write_text(analysis.model_dump_json(indent=2))
    return analysis
