"""Remove personal data from the still frames so the output can be shared.

The Personal Data sheet says what the frames show and where. This module
paints over those spots on each still (an opaque box by default, or a heavy
blur), keeps an untouched copy of every frame it changes under
`frames/original/`, regenerates the close-up for the frame, masks the same
values wherever the analysis text carries them, and re-reads the redacted
frames so the frame-text record is clean too.

The raw values are needed here, once, to find them on the frame and in the
analysis text. They live only in `_Located`, a private structure that is
never written to disk and never placed on a `PiiHit`.
"""
from __future__ import annotations

import json
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional

from PIL import Image, ImageDraw, ImageFilter
from pydantic import BaseModel, Field

from specto.diff import crop_path_for, crop_region, crop_wanted
from specto.model import Analysis, Keyframe, Recording
from specto.ocr import OCR_CACHE_NAME, TextLine, ocr_available, ocr_image
from specto.pii import (
    Box,
    PiiHit,
    _box_for_span,
    _detect,
    _joined,
    _line_groups,
    _mask_pairs,
    _place_in_lines,
    mask_value,
)

Style = Literal["box", "blur"]

LINES_CACHE_NAME = "ocr_lines.json"
ORIGINAL_DIR = "original"
BOX_COLOUR = (40, 40, 40)
NOT_INSTALLED_ERROR = (
    "Redaction needs the OCR engine to find the text on each frame, and it is not installed. "
    "Run: pip install \"specto[ocr]\""
)


class RedactReport(BaseModel):
    """What one redaction run changed."""

    frames_touched: int = 0
    boxes_painted: int = 0
    text_replacements: int = Field(default=0, description="Values masked inside analysis.json")
    kinds: dict[str, int] = Field(default_factory=dict, description="How many boxes of each kind of personal data")


@dataclass
class _Located:
    """One value found on a frame. Private: the raw text never leaves this module."""

    hit: PiiHit
    pieces: list[tuple[str, str]] = field(default_factory=list)  # (raw, masked), longest first


# The raw pieces behind the hits `find_redactions` returned, so `scrub_analysis`
# can take plain hits. Keyed on what a hit carries; lives in memory only.
_RAW_PIECES: dict[tuple, list[tuple[str, str]]] = {}


def _hit_key(hit: PiiHit) -> tuple:
    return (hit.kind, hit.value_masked, hit.keyframe_index, hit.box)


# ------------------------------------------------------------------ OCR lines


def ocr_lines_for_recording(
    recording: Recording,
    out_dir: Path | str,
    log: Callable[[str], None] = print,
) -> dict[int, list[TextLine]]:
    """The text lines, with boxes, for every keyframe; keyframe index -> lines.

    Cached in `out_dir/ocr_lines.json` so a re-run is free. Raises a
    RuntimeError naming the install command when OCR is not installed.
    """
    out_dir = Path(out_dir)
    cache_path = out_dir / LINES_CACHE_NAME
    if cache_path.exists():
        log(f"reusing {cache_path}")
        raw = json.loads(cache_path.read_text())
        return {int(k): [TextLine.model_validate(item) for item in v] for k, v in raw.items()}

    if not ocr_available():
        raise RuntimeError(NOT_INSTALLED_ERROR)

    lines_by_frame: dict[int, list[TextLine]] = {}
    for keyframe in recording.keyframes:
        lines_by_frame[keyframe.index] = ocr_image(out_dir / keyframe.path)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {str(k): [line.model_dump() for line in v] for k, v in lines_by_frame.items()},
            indent=1,
            ensure_ascii=False,
        )
    )
    log(f"ocr: {len(lines_by_frame)} frames read for redaction, written to {cache_path}")
    return lines_by_frame


# ---------------------------------------------------------------- locating


def _boxes_of_value(lines: list[TextLine], raw: str) -> list[Box]:
    """Every box on the frame whose line(s) contain `raw`.

    Spaces are ignored on both sides: the engine often reads "SW1A 1AA" as
    "SW1A1AA", and a value may sit in two boxes on one row.
    """
    needle = "".join(raw.split())
    if not needle:
        return []
    boxes: list[Box] = []
    for group, separators in _line_groups(lines):
        text, spans = _joined(group, separators)
        positions = [i for i, ch in enumerate(text) if not ch.isspace()]
        compact = "".join(text[i] for i in positions)
        start = 0
        while (at := compact.find(needle, start)) != -1:
            box = _box_for_span(group, spans, positions[at], positions[at + len(needle) - 1] + 1)
            if not any(_overlap(box, b) for b in boxes):
                boxes.append(box)
            start = at + len(needle)
    return boxes


def _overlap(a: Box, b: Box) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _locate(
    recording: Recording,
    analysis: Analysis,
    lines_by_frame: dict[int, list[TextLine]],
) -> list[_Located]:
    """Every value to paint over, with its box and (privately) its raw text."""
    times = {kf.index: kf.timestamp for kf in recording.keyframes}
    located: list[_Located] = []
    seen: set[tuple] = set()

    def add(hit: PiiHit, pieces: list[tuple[str, str]]) -> None:
        key = _hit_key(hit)
        if key in seen:
            return
        for item in located:
            # The same spot found twice (the frame scan and the example-value
            # search read the value slightly differently): one hit, both raws.
            same_spot = (
                item.hit.kind == hit.kind
                and item.hit.keyframe_index == hit.keyframe_index
                and item.hit.box is not None and hit.box is not None
                and _overlap(item.hit.box, hit.box)
            )
            if same_spot:
                item.pieces = sorted(set(item.pieces) | set(pieces), key=lambda p: -len(p[0]))
                return
        seen.add(key)
        located.append(_Located(hit, pieces))

    for index, lines in sorted(lines_by_frame.items()):
        for p in _place_in_lines(lines):
            hit = PiiHit(
                kind=p.found.kind,
                value_masked=mask_value(p.found.kind, p.found.raw),
                source="frame text",
                keyframe_index=index,
                timestamp=times.get(index),
                box=p.box,
            )
            add(hit, _mask_pairs([p.found]))

    for f in analysis.fields:
        if not f.example_value or f.keyframe_index not in lines_by_frame:
            continue
        for found in _detect(f.example_value):
            pieces = _mask_pairs([found])
            for raw, _masked in pieces:
                for box in _boxes_of_value(lines_by_frame[f.keyframe_index], raw):
                    hit = PiiHit(
                        kind=found.kind,
                        value_masked=mask_value(found.kind, found.raw),
                        source="example value",
                        keyframe_index=f.keyframe_index,
                        timestamp=f.timestamp,
                        box=box,
                    )
                    add(hit, pieces)
    return located


def find_redactions(
    recording: Recording,
    analysis: Analysis,
    lines_by_frame: dict[int, list[TextLine]],
) -> list[PiiHit]:
    """Every hit that has a box on a frame: from the frame text, and from field example values.

    The example value in the analysis is searched for on the field's frame.
    It is a search key only; nothing here writes it out.
    """
    located = _locate(recording, analysis, lines_by_frame)
    for item in located:
        _RAW_PIECES[_hit_key(item.hit)] = item.pieces
    return [item.hit for item in located]


# ----------------------------------------------------------------- painting


def _paint(img: Image.Image, box: Box, style: Style, pad: int) -> None:
    left = max(0, box[0] - pad)
    top = max(0, box[1] - pad)
    right = min(img.width, box[0] + box[2] + pad)
    bottom = min(img.height, box[1] + box[3] + pad)
    if right <= left or bottom <= top:
        return
    if style == "box":
        ImageDraw.Draw(img).rectangle([left, top, right - 1, bottom - 1], fill=BOX_COLOUR)
        return
    region = img.crop((left, top, right, bottom))
    w, h = region.size
    # Pixelate first, then blur: a big enough block size that no letter shape survives.
    block = max(12, h // 2)
    small = region.resize((max(1, w // block), max(1, h // block)), Image.Resampling.BOX)
    region = small.resize((w, h), Image.Resampling.NEAREST)
    region = region.filter(ImageFilter.GaussianBlur(radius=max(6, h / 3)))
    img.paste(region, (left, top))


def _original_path(out_dir: Path, keyframe: Keyframe) -> Path:
    frame = Path(keyframe.path)
    return out_dir / frame.parent / ORIGINAL_DIR / frame.name


def _save(img: Image.Image, path: Path) -> None:
    if path.suffix.lower() in (".jpg", ".jpeg"):
        img.save(path, "JPEG", quality=92)
    else:
        img.save(path)


def _refresh_crop(out_dir: Path, keyframe: Keyframe, width: int, height: int) -> None:
    crop_path = out_dir / crop_path_for(keyframe.path)
    if crop_path.exists():
        crop_path.unlink()
    region = keyframe.change_from_previous
    if region is not None and crop_wanted(region, width, height):
        crop_region(out_dir / keyframe.path, region, crop_path)


def redact_frames(
    recording: Recording,
    out_dir: Path | str,
    hits: list[PiiHit],
    style: Style = "box",
    pad: int = 4,
    log: Callable[[str], None] = print,
) -> int:
    """Paint over every hit's box on its frame; return how many boxes were painted.

    "box" paints an opaque dark-grey rectangle, which cannot be undone by
    sharpening. "blur" pixelates and blurs the spot. Each box is grown by
    `pad` pixels on every side. The untouched frame is copied to
    `frames/original/` the first time it is redacted; the frame is then
    overwritten and its close-up regenerated from the redacted image. Running
    twice paints the same boxes again, which changes nothing.
    """
    out_dir = Path(out_dir)
    by_frame: dict[int, list[Box]] = {}
    for hit in hits:
        if hit.box is not None and hit.keyframe_index is not None:
            by_frame.setdefault(hit.keyframe_index, []).append(hit.box)

    painted = 0
    for keyframe in recording.keyframes:
        boxes = by_frame.get(keyframe.index)
        if not boxes:
            continue
        frame_path = out_dir / keyframe.path
        if not frame_path.exists():
            log(f"redact: frame {keyframe.index} is missing at {frame_path}, skipped")
            continue
        original = _original_path(out_dir, keyframe)
        if not original.exists():
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(frame_path, original)
        with Image.open(frame_path) as opened:
            img = opened.convert("RGB")
        for box in boxes:
            _paint(img, box, style, pad)
        _save(img, frame_path)
        _refresh_crop(out_dir, keyframe, img.width, img.height)
        painted += len(boxes)
        log(f"redact: frame {keyframe.index}: {len(boxes)} spot{'s' if len(boxes) != 1 else ''} painted ({style})")
    return painted


# ---------------------------------------------------------------- scrubbing


def _scrub_value(value, pieces: list[tuple[str, str]]) -> tuple[object, int]:
    """Replace every raw piece inside a string, list or model; return (new value, count)."""
    if isinstance(value, str):
        count = 0
        for raw, masked in pieces:
            n = value.count(raw)
            if n:
                value = value.replace(raw, masked)
                count += n
        return value, count
    if isinstance(value, list):
        total = 0
        out = []
        for item in value:
            new, n = _scrub_value(item, pieces)
            out.append(new)
            total += n
        return out, total
    if isinstance(value, BaseModel):
        total = 0
        for name in type(value).model_fields:
            new, n = _scrub_value(getattr(value, name), pieces)
            if n:
                setattr(value, name, new)
                total += n
        return value, total
    return value, 0


def scrub_analysis(analysis: Analysis, hits: list[PiiHit]) -> tuple[Analysis, int]:
    """Mask the located values wherever the analysis text carries them.

    Example values, source quotes, context quotes, notes, question text and
    every other free-text field are checked. Only hits that came out of
    `find_redactions` in this process can be scrubbed, because only those
    have their raw text on hand. Returns a new Analysis and the number of
    replacements made.
    """
    pieces: dict[str, str] = {}
    for hit in hits:
        for raw, masked in _RAW_PIECES.get(_hit_key(hit), []):
            pieces.setdefault(raw, masked)
    if not pieces:
        return analysis.model_copy(deep=True), 0
    ordered = sorted(pieces.items(), key=lambda p: -len(p[0]))
    scrubbed, count = _scrub_value(analysis.model_copy(deep=True), ordered)
    return scrubbed, count


# --------------------------------------------------------------- the folder


def _load_ocr_text(out_dir: Path, lines_by_frame: dict[int, list[TextLine]]) -> dict[str, str]:
    path = out_dir / OCR_CACHE_NAME
    if path.exists():
        return {str(k): str(v) for k, v in json.loads(path.read_text()).items()}
    return {str(k): "\n".join(line.text for line in v) for k, v in sorted(lines_by_frame.items())}


def redact_dir(
    out_dir: Path | str,
    style: Style = "box",
    log: Callable[[str], None] = print,
) -> RedactReport:
    """Redact every frame in an output folder and update what refers to the frames.

    Reads recording.json and analysis.json, finds every personal-data spot on
    the frames, paints over them, masks the same values inside analysis.json,
    and re-reads the redacted frames so ocr.json holds clean text. Does not
    export; the caller re-exports afterwards.
    """
    out_dir = Path(out_dir)
    recording = Recording.model_validate_json((out_dir / "recording.json").read_text())
    analysis_path = out_dir / "analysis.json"
    analysis = Analysis.model_validate_json(analysis_path.read_text())

    lines_by_frame = ocr_lines_for_recording(recording, out_dir, log=log)
    hits = find_redactions(recording, analysis, lines_by_frame)
    report = RedactReport(kinds=dict(sorted(Counter(hit.kind for hit in hits).items())))
    if not hits:
        log("redact: no personal data found on the frames")
        return report

    report.boxes_painted = redact_frames(recording, out_dir, hits, style=style, log=log)
    touched = sorted({hit.keyframe_index for hit in hits if hit.keyframe_index is not None})
    report.frames_touched = len(touched)

    analysis, report.text_replacements = scrub_analysis(analysis, hits)
    analysis_path.write_text(analysis.model_dump_json(indent=2))

    ocr_text = _load_ocr_text(out_dir, lines_by_frame)
    by_index = {kf.index: kf for kf in recording.keyframes}
    for index in touched:
        keyframe = by_index.get(index)
        if keyframe is None or not (out_dir / keyframe.path).exists():
            continue
        if ocr_available():
            ocr_text[str(index)] = "\n".join(line.text for line in ocr_image(out_dir / keyframe.path))
        else:
            ocr_text[str(index)] = ""
    (out_dir / OCR_CACHE_NAME).write_text(json.dumps(ocr_text, indent=2, ensure_ascii=False))

    log(
        f"redact: {report.boxes_painted} spot{'s' if report.boxes_painted != 1 else ''} painted on "
        f"{report.frames_touched} frame{'s' if report.frames_touched != 1 else ''}, "
        f"{report.text_replacements} value{'s' if report.text_replacements != 1 else ''} masked in analysis.json; "
        f"untouched frames kept under {out_dir / 'frames' / ORIGINAL_DIR}"
    )
    return report


def restore_dir(out_dir: Path | str) -> int:
    """Put the untouched frames from `frames/original/` back; return how many.

    The close-ups are regenerated from the restored frames and ocr.json goes
    back to the text read off them, when recording.json and the line cache
    are present.
    """
    out_dir = Path(out_dir)
    original_dir = out_dir / "frames" / ORIGINAL_DIR
    if not original_dir.is_dir():
        return 0
    restored = 0
    names: set[str] = set()
    for path in sorted(original_dir.iterdir()):
        if not path.is_file():
            continue
        shutil.copy2(path, out_dir / "frames" / path.name)
        names.add(path.name)
        restored += 1

    recording_path = out_dir / "recording.json"
    if restored and recording_path.exists():
        recording = Recording.model_validate_json(recording_path.read_text())
        lines_cache = out_dir / LINES_CACHE_NAME
        lines_by_frame = (
            ocr_lines_for_recording(recording, out_dir, log=lambda _: None) if lines_cache.exists() else {}
        )
        ocr_path = out_dir / OCR_CACHE_NAME
        ocr_text = json.loads(ocr_path.read_text()) if ocr_path.exists() else {}
        for keyframe in recording.keyframes:
            if Path(keyframe.path).name not in names:
                continue
            with Image.open(out_dir / keyframe.path) as img:
                width, height = img.size
            _refresh_crop(out_dir, keyframe, width, height)
            if keyframe.index in lines_by_frame:
                ocr_text[str(keyframe.index)] = "\n".join(line.text for line in lines_by_frame[keyframe.index])
        if ocr_path.exists() or lines_by_frame:
            ocr_path.write_text(json.dumps(ocr_text, indent=2, ensure_ascii=False))
    return restored
