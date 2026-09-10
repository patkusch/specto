"""Optional text reading (OCR) of each still frame.

The model gets each frame as an image, and images are downscaled, so small
labels, table headers and values can be hard to read. This module reads the
text off the full-size frame with a local OCR engine so it can be handed to
the model next to the image, and so there is a "text seen on screen" record
for each frame that needs no model at all.

The engine is rapidocr-onnxruntime: pure pip, models bundled, no system
binary. It is an optional extra (`pip install "specto[ocr]"`); everything
here degrades to "no text" when it is not installed.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, Field

from specto.model import Recording

OCR_CACHE_NAME = "ocr.json"
NOT_INSTALLED_NOTE = "OCR is not installed, so frame text is skipped (pip install \"specto[ocr]\")"

_engine = None
_engine_error: Optional[BaseException] = None


class TextLine(BaseModel):
    """One row of text on a frame, in pixel coordinates of the image."""

    text: str
    x: int = Field(description="Left edge in pixels")
    y: int = Field(description="Top edge in pixels")
    w: int = Field(description="Width in pixels")
    h: int = Field(description="Height in pixels")
    confidence: float = Field(description="0 to 1, from the engine")

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h


# ---------------------------------------------------------------------- engine


def _load_engine():
    """Import and build the OCR engine once; raise if it cannot be loaded."""
    global _engine, _engine_error
    if _engine is not None:
        return _engine
    if _engine_error is not None:
        raise _engine_error
    try:
        from rapidocr_onnxruntime import RapidOCR

        _engine = RapidOCR()
    except BaseException as error:  # ImportError, or a missing shared library
        _engine_error = error
        raise
    return _engine


def ocr_available() -> bool:
    """True when the OCR engine is installed and loads on this machine."""
    try:
        _load_engine()
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------- frames


def merge_rows(lines: list[TextLine], gap: str = "  ") -> list[TextLine]:
    """Join words that sit on the same row into one line, left to right.

    Two boxes are on the same row when the vertical centre of one falls
    inside the other's band. Rows come out top to bottom; within a row the
    texts are joined with `gap`, so a label and its value stay together.
    """
    rows: list[list[TextLine]] = []
    for line in sorted(lines, key=lambda l: (l.y, l.x)):
        centre = line.y + line.h / 2
        for row in rows:
            top = min(l.y for l in row)
            bottom = max(l.bottom for l in row)
            row_centre = (top + bottom) / 2
            if top <= centre <= bottom or line.y <= row_centre <= line.bottom:
                row.append(line)
                break
        else:
            rows.append([line])

    merged: list[TextLine] = []
    for row in rows:
        row.sort(key=lambda l: l.x)
        x = min(l.x for l in row)
        y = min(l.y for l in row)
        merged.append(
            TextLine(
                text=gap.join(l.text for l in row),
                x=x,
                y=y,
                w=max(l.right for l in row) - x,
                h=max(l.bottom for l in row) - y,
                confidence=sum(l.confidence for l in row) / len(row),
            )
        )
    merged.sort(key=lambda l: (l.y, l.x))
    return merged


def _raw_lines(path: Path) -> list[TextLine]:
    """Every text box the engine found, unordered and unmerged."""
    engine = _load_engine()
    result, _elapsed = engine(str(path))
    lines: list[TextLine] = []
    for item in result or []:
        # [box, text, score] in every rapidocr-onnxruntime release; newer ones may
        # append more, and the score is a str in 1.2.x and a float in 1.4.x.
        box, text, confidence = item[0], item[1], item[2]
        text = str(text).strip()
        if not text:
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        x, y = int(round(min(xs))), int(round(min(ys)))
        lines.append(
            TextLine(
                text=text,
                x=x,
                y=y,
                w=max(1, int(round(max(xs))) - x),
                h=max(1, int(round(max(ys))) - y),
                confidence=float(confidence),
            )
        )
    return lines


def ocr_image(path: Path | str) -> list[TextLine]:
    """Read the text on one image, one TextLine per row, top to bottom.

    Raises FileNotFoundError when the image is missing and whatever the engine
    raises when it is not installed; check `ocr_available()` first.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Frame image not found: {path}")
    return merge_rows(_raw_lines(path))


# ------------------------------------------------------------------- recording


def ocr_recording(
    recording: Recording,
    out_dir: Path | str,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> dict[int, str]:
    """Text for every keyframe, keyframe_index -> one line per row.

    Cached in `out_dir/ocr.json` (a JSON object with string keys) and reused on
    a re-run unless `force`. When OCR is not installed, one line is logged and
    an empty dict comes back; nothing is written, so a later run with OCR
    installed does the work.
    """
    out_dir = Path(out_dir)
    cache_path = out_dir / OCR_CACHE_NAME
    if cache_path.exists() and not force:
        log(f"reusing {cache_path}")
        return {int(k): str(v) for k, v in json.loads(cache_path.read_text()).items()}

    if not ocr_available():
        log(NOT_INSTALLED_NOTE)
        return {}

    started = time.perf_counter()
    text_by_frame: dict[int, str] = {}
    for keyframe in recording.keyframes:
        lines = ocr_image(out_dir / keyframe.path)
        text_by_frame[keyframe.index] = "\n".join(line.text for line in lines)

    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({str(k): v for k, v in text_by_frame.items()}, indent=2, ensure_ascii=False)
    )
    with_text = sum(1 for text in text_by_frame.values() if text)
    log(
        f"ocr: {len(text_by_frame)} frames read, {with_text} with text, "
        f"{time.perf_counter() - started:.1f}s, written to {cache_path}"
    )
    return text_by_frame
