"""A cost estimate for the extract stage, worked out from the recording alone.

Nothing here calls the model. The estimate counts what `specto.extract` would
send: one call per chunk of frames (each frame as an image, its close-up crop
when there is one, its OCR text, and the words said over it) and then one
consolidation call over the whole transcript and every chunk reading. Token
counts use the usual rules of thumb (an image costs about width * height / 750
tokens; text costs about one token per four characters) and the prices in
PRICES, so the answer is a rough guide, not a quote. The real usage is printed
after the run.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image
from pydantic import BaseModel, Field

from specto.diff import crop_path_for
from specto.extract import LONG_TRANSCRIPT_CHARS, OCR_TEXT_MAX_CHARS, format_transcript
from specto.model import Moment, Recording
from specto.prompts import SYSTEM_PROMPT_CONSOLIDATE, SYSTEM_PROMPT_READ
from specto.timefmt import mmss

DEFAULT_MODEL = "claude-opus-5"

# Dollars per million tokens (input, output). Checked on 2026-09-10 against the
# Claude API price list. Cache reads cost 10% of the input price.
PRICES_CHECKED_ON = "2026-09-10"
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
}
CACHE_READ_FRACTION = 0.10
COMPARED_MODELS = ("claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5", "claude-fable-5-1")

# Rules of thumb, all in tokens.
IMAGE_PIXELS_PER_TOKEN = 750
CHARS_PER_TOKEN = 4
CHUNK_HEADER_TOKENS = 120  # the chunk header, the known-screens list and the per-frame labels
CHUNK_READING_TOKENS = 1200  # what one chunk reading weighs when sent back for consolidation
CHUNK_OUTPUT_TOKENS = 1500
CONSOLIDATION_OUTPUT_TOKENS = 4000
FALLBACK_SIZE = (1280, 720)


class Estimate(BaseModel):
    model: str
    calls: int = 0
    images: int = Field(default=0, description="Frames plus close-up crops that would be sent")
    image_tokens: int = 0
    text_tokens: int = Field(default=0, description="Uncached input text, including each system prompt once")
    cached_tokens: int = Field(default=0, description="System prompt tokens served from the cache on later calls")
    output_tokens: int = 0
    input_cost_usd: float = 0.0
    output_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    recording_seconds: float = 0.0
    notes: list[str] = Field(default_factory=list)

    @property
    def cost_per_hour_usd(self) -> Optional[float]:
        """Cost divided by the recording length in hours; None for an empty recording."""
        if self.recording_seconds <= 0:
            return None
        return self.total_cost_usd / (self.recording_seconds / 3600.0)


# --------------------------------------------------------------------- counting


def _text_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _image_tokens(width: int, height: int) -> int:
    return max(1, round(width * height / IMAGE_PIXELS_PER_TOKEN))


def _image_size(out_dir: Path, rel_path: str) -> Optional[tuple[int, int]]:
    """Width and height of an image on disk, or None when it cannot be opened."""
    try:
        with Image.open(out_dir / rel_path) as img:
            return img.size
    except Exception:
        return None


def _frame_size(keyframe, out_dir: Path) -> tuple[tuple[int, int], bool]:
    """The frame's stored size, else the JPEG's, else the fallback; the flag says the fallback was used."""
    if keyframe.width and keyframe.height:
        return (keyframe.width, keyframe.height), False
    size = _image_size(out_dir, keyframe.path)
    if size is not None:
        return size, False
    return FALLBACK_SIZE, True


def _ocr_tokens(text: str) -> int:
    text = text.strip()
    if not text:
        return 0
    return _text_tokens(text[:OCR_TEXT_MAX_CHARS]) + 12  # the "Text read from frame N" label


def _moments(recording: Recording) -> tuple[list[Moment], Optional[str]]:
    """The moments extract would chunk; one per keyframe when ingest left none."""
    if recording.moments:
        return recording.moments, None
    if not recording.keyframes:
        return [], None
    moments = [
        Moment(keyframe_index=k.index, start=k.timestamp, end=k.timestamp) for k in recording.keyframes
    ]
    return moments, "The recording has no moments, so every frame was counted with nothing said over it."


# --------------------------------------------------------------------- estimate


def estimate(
    recording: Recording,
    out_dir: Path | str,
    model: str = DEFAULT_MODEL,
    frames_per_call: int = 8,
    ocr_text: Optional[dict[int, str]] = None,
) -> Estimate:
    """Estimate what `extract` would spend on this recording, without calling the model.

    `ocr_text` maps keyframe_index to the text read off that frame, the same
    dict `specto.ocr.ocr_recording` returns; leave it out to estimate a run
    without OCR. Crops are counted when `frames/crop_NNNN.jpg` exists next to
    the frame in `out_dir`.
    """
    out_dir = Path(out_dir)
    ocr_text = ocr_text or {}
    frames_per_call = max(1, frames_per_call)
    est = Estimate(model=model, recording_seconds=recording.duration)

    if model in PRICES:
        input_price, output_price = PRICES[model]
    else:
        input_price, output_price = PRICES[DEFAULT_MODEL]
        est.notes.append(
            f"No price is known for {model}; the {DEFAULT_MODEL} prices were used."
        )

    moments, note = _moments(recording)
    if note:
        est.notes.append(note)
    if not moments:
        est.notes.append("The recording has no frames, so extract would make no calls.")
        return est

    keyframes = {k.index: k for k in recording.keyframes}
    chunks = [moments[i : i + frames_per_call] for i in range(0, len(moments), frames_per_call)]
    read_prompt_tokens = _text_tokens(SYSTEM_PROMPT_READ)
    consolidate_prompt_tokens = _text_tokens(SYSTEM_PROMPT_CONSOLIDATE)

    # Pass 1: one call per chunk. The system prompt is written to the cache on
    # the first call and read back on the rest.
    assumed_size = 0
    for number, chunk in enumerate(chunks):
        est.calls += 1
        est.text_tokens += CHUNK_HEADER_TOKENS
        est.text_tokens += read_prompt_tokens if number == 0 else 0
        est.cached_tokens += read_prompt_tokens if number > 0 else 0
        est.output_tokens += CHUNK_OUTPUT_TOKENS
        for moment in chunk:
            keyframe = keyframes.get(moment.keyframe_index)
            if keyframe is None:
                (width, height), assumed = FALLBACK_SIZE, True
                crop_size = None
            else:
                (width, height), assumed = _frame_size(keyframe, out_dir)
                crop_rel = crop_path_for(keyframe.path)
                crop_size = _image_size(out_dir, crop_rel) if (out_dir / crop_rel).exists() else None
            assumed_size += 1 if assumed else 0
            est.images += 1
            est.image_tokens += _image_tokens(width, height)
            if crop_size is not None:
                est.images += 1
                est.image_tokens += _image_tokens(*crop_size)
                est.text_tokens += 30  # the "where it changed" line and the close-up label
            est.text_tokens += _ocr_tokens(ocr_text.get(moment.keyframe_index, ""))
            est.text_tokens += _text_tokens(moment.text or "(nothing said)") + 12
    if assumed_size:
        est.notes.append(
            f"{assumed_size} frame(s) had no size on record and no image on disk; "
            f"{FALLBACK_SIZE[0]}x{FALLBACK_SIZE[1]} was assumed."
        )

    # Pass 2: consolidation over the full transcript and every chunk reading.
    transcript = format_transcript(recording)
    consolidation_calls = 1 if len(transcript) <= LONG_TRANSCRIPT_CHARS else 2
    if consolidation_calls == 2:
        est.notes.append(
            "The transcript is long, so consolidation is split into two calls, as extract does."
        )
    consolidation_input = _text_tokens(transcript) + CHUNK_READING_TOKENS * len(chunks) + 60
    for number in range(consolidation_calls):
        est.calls += 1
        est.text_tokens += consolidation_input
        est.text_tokens += consolidate_prompt_tokens if number == 0 else 0
        est.cached_tokens += consolidate_prompt_tokens if number > 0 else 0
        est.output_tokens += CONSOLIDATION_OUTPUT_TOKENS

    est.input_cost_usd = (
        (est.image_tokens + est.text_tokens) * input_price
        + est.cached_tokens * input_price * CACHE_READ_FRACTION
    ) / 1_000_000
    est.output_cost_usd = est.output_tokens * output_price / 1_000_000
    est.total_cost_usd = est.input_cost_usd + est.output_cost_usd
    return est


def compare_models(
    recording: Recording,
    out_dir: Path | str,
    frames_per_call: int = 8,
    ocr_text: Optional[dict[int, str]] = None,
    models: tuple[str, ...] = COMPARED_MODELS,
) -> list[Estimate]:
    """The same estimate on each model in `models`, cheapest first."""
    return [
        estimate(recording, out_dir, model=model, frames_per_call=frames_per_call, ocr_text=ocr_text)
        for model in models
    ]


# ------------------------------------------------------------------- formatting


def _k(tokens: int) -> str:
    """118_400 -> '118k'; 850 -> '850'."""
    if tokens >= 10_000:
        return f"{round(tokens / 1000)}k"
    if tokens >= 1_000:
        return f"{tokens / 1000:.1f}k"
    return str(tokens)


def _money(usd: float) -> str:
    return f"${usd:.2f}" if usd >= 0.10 else f"${usd:.3f}"


def _hourly_line(est: Estimate) -> Optional[str]:
    per_hour = est.cost_per_hour_usd
    if per_hour is None:
        return None
    return (
        f"That is about {_money(per_hour)} per hour of recording "
        f"(this one is {mmss(est.recording_seconds)} long)."
    )


def format_estimate(est: Estimate) -> str:
    """Three or four plain lines a person can read before spending money."""
    lines = [
        f"Estimated cost on {est.model}: {_money(est.total_cost_usd)} "
        f"({est.calls} model calls; {est.images} images ≈ {_k(est.image_tokens)} tokens, "
        f"text ≈ {_k(est.text_tokens)} tokens, cached ≈ {_k(est.cached_tokens)} tokens, "
        f"output ≈ {_k(est.output_tokens)} tokens)."
    ]
    hourly = _hourly_line(est)
    if hourly:
        lines.append(hourly)
    lines.append(
        f"Estimates are rough; the real usage is printed after the run. "
        f"Prices checked on {PRICES_CHECKED_ON}."
    )
    lines.extend(f"Note: {note}" for note in est.notes)
    return "\n".join(lines)


def format_comparison(estimates: list[Estimate]) -> str:
    """One line per model, plus the shared token counts once."""
    if not estimates:
        return "Nothing to compare."
    first = estimates[0]
    width = max(len(e.model) for e in estimates)
    lines = [
        f"Estimated cost of the same run on each model ({first.calls} model calls; "
        f"{first.images} images ≈ {_k(first.image_tokens)} tokens, text ≈ {_k(first.text_tokens)} tokens, "
        f"output ≈ {_k(first.output_tokens)} tokens):"
    ]
    for est in estimates:
        per_hour = est.cost_per_hour_usd
        hourly = f"  ({_money(per_hour)} per hour of recording)" if per_hour is not None else ""
        lines.append(f"  {est.model.ljust(width)}  {_money(est.total_cost_usd):>8}{hourly}")
    lines.append(
        f"Estimates are rough; the real usage is printed after the run. "
        f"Prices checked on {PRICES_CHECKED_ON}."
    )
    notes = {note for est in estimates for note in est.notes}
    lines.extend(f"Note: {note}" for note in sorted(notes))
    return "\n".join(lines)
