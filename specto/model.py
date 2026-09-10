"""Shared data model. Every stage reads and writes these shapes.

Timestamps are seconds from the start of the recording. Frame references use
`keyframe_index`, which is the index into `Recording.keyframes`; the exporter
turns that into a link to the still image on disk.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- stage 1: ingest


class Word(BaseModel):
    start: float
    end: float
    text: str


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str
    speaker: Optional[str] = None
    words: list[Word] = Field(default_factory=list, description="Word-level timings when the transcriber gives them; empty otherwise")


class ChangedRegion(BaseModel):
    """Where a still differs from the previous still, in pixels of the saved frame."""

    x: int
    y: int
    w: int
    h: int
    fraction: float = Field(description="Share of the frame area that changed, 0 to 1")


class Keyframe(BaseModel):
    index: int
    timestamp: float
    path: str = Field(description="Path relative to the output folder, e.g. frames/frame_0007.jpg")
    phash: Optional[str] = Field(default=None, description="Perceptual hash, hex, used for near-duplicate removal")
    width: Optional[int] = None
    height: Optional[int] = None
    change_from_previous: Optional[ChangedRegion] = Field(default=None, description="None for the first frame or when nothing measurable changed")


class Moment(BaseModel):
    """One still frame plus everything said while it was on screen."""

    keyframe_index: int
    start: float
    end: float
    segments: list[TranscriptSegment] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())


class Recording(BaseModel):
    source: str = Field(description="Original video path or name")
    duration: float
    keyframes: list[Keyframe] = Field(default_factory=list)
    segments: list[TranscriptSegment] = Field(default_factory=list)
    moments: list[Moment] = Field(default_factory=list)
    transcript_source: Literal["file", "whisper", "none"] = "none"


# --------------------------------------------------------------- stage 2: extract


class DataField(BaseModel):
    id: str = Field(description="Stable id, e.g. F001")
    screen_id: str
    label: str = Field(description="The label as shown on screen or as the expert named it")
    field_type: str = Field(description="text, number, date, dropdown, checkbox, radio, table column, file, read-only, other")
    required: Optional[bool] = Field(default=None, description="True/False if known, null if not stated")
    example_value: Optional[str] = Field(default=None, description="A value visible on screen, if any; never invent one")
    source: Literal["seen on screen", "mentioned by expert", "both"]
    timestamp: float
    keyframe_index: int
    notes: Optional[str] = None


class Action(BaseModel):
    id: str = Field(description="Stable id, e.g. A001")
    screen_id: str
    description: str = Field(description="What the user does, e.g. 'Presses Save'")
    control: Optional[str] = Field(default=None, description="button, link, menu, tab, keyboard, other")
    leads_to_screen_id: Optional[str] = None
    timestamp: float
    keyframe_index: int


class Screen(BaseModel):
    id: str = Field(description="Stable id, e.g. S01")
    name: str
    purpose: str = Field(description="One sentence: what this screen is for")
    keyframe_indexes: list[int] = Field(default_factory=list)
    first_seen: float
    field_ids: list[str] = Field(default_factory=list)
    action_ids: list[str] = Field(default_factory=list)


class JourneyStep(BaseModel):
    order: int
    screen_id: str
    description: str = Field(description="One sentence, what happens at this step")
    actor: Optional[str] = Field(default=None, description="Who does it, if known")
    timestamp: float
    keyframe_index: int


class Requirement(BaseModel):
    id: str = Field(description="Stable id, e.g. R001")
    statement: str = Field(description="'As a <role>, I need <thing>, so that <benefit>' or a plain 'The system must ...' sentence")
    rationale: Optional[str] = None
    source_quote: str = Field(description="The expert's words this came from, verbatim or near-verbatim")
    timestamp: float
    keyframe_index: int
    screen_id: Optional[str] = None
    kind: Literal["functional", "data", "validation", "workflow", "non-functional", "reporting"] = "functional"
    priority: Literal["must", "should", "could", "unknown"] = "unknown"
    confidence: Literal["high", "medium", "low"] = Field(description="high = the expert said it plainly; low = inferred from the screen")


class AcceptanceCriterion(BaseModel):
    id: str = Field(description="Stable id, e.g. AC001")
    requirement_id: str
    given: str
    when: str
    then: str
    timestamp: float
    keyframe_index: int


class Question(BaseModel):
    id: str = Field(description="Stable id, e.g. Q001")
    question: str
    why_it_matters: str
    context_quote: Optional[str] = Field(default=None, description="What the expert said that raised it")
    timestamp: float
    keyframe_index: int
    screen_id: Optional[str] = None
    category: Literal["ambiguity", "missing information", "edge case", "validation rule", "permissions", "data", "integration", "other"] = "other"


class Usage(BaseModel):
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class Analysis(BaseModel):
    title: str
    summary: str = Field(description="Three to five plain sentences on what the process is and who uses it")
    actors: list[str] = Field(default_factory=list)
    screens: list[Screen] = Field(default_factory=list)
    fields: list[DataField] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    journey: list[JourneyStep] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    usage: Optional[Usage] = None
