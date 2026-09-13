"""Find personal data in what the recording captured, and say where it was.

An expert walking through a real system shows real customer records. The
frames, the text read off them, the transcript and the example values in the
workbook then hold personal data, and the analyst must know that before the
output is shared or stored. This module lists what was seen and on which frame,
with every value masked, so the analyst can check it and remove frames if
needed.

Nothing here writes an unmasked value: `PiiHit` only carries the masked form,
and the context snippet has every found value masked inside it too.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Optional

from pydantic import BaseModel, Field

from specto.model import Analysis, Recording
from specto.ocr import OCR_CACHE_NAME, TextLine
from specto.timefmt import mmss

Kind = Literal[
    "email",
    "phone",
    "uk postcode",
    "date of birth",
    "national insurance number",
    "card number",
    "iban",
    "sort code and account",
    "person name",
    "address",
    "other id",
]
Source = Literal["frame text", "transcript", "example value"]

KINDS: list[str] = list(Kind.__args__)  # type: ignore[attr-defined]

# What each kind is called in the summary line, singular and plural.
KIND_WORDS: dict[str, tuple[str, str]] = {
    "email": ("email", "emails"),
    "phone": ("phone number", "phone numbers"),
    "uk postcode": ("postcode", "postcodes"),
    "date of birth": ("date of birth", "dates of birth"),
    "national insurance number": ("National Insurance number", "National Insurance numbers"),
    "card number": ("card number", "card numbers"),
    "iban": ("IBAN", "IBANs"),
    "sort code and account": ("sort code and account", "sort codes and accounts"),
    "person name": ("name", "names"),
    "address": ("address", "addresses"),
    "other id": ("other id", "other ids"),
}

Box = tuple[int, int, int, int]  # x, y, w, h in pixels of the saved frame

CONTEXT_CHARS = 60
PII_HEADERS = ["Kind", "Value (masked)", "Where", "Time", "Frame", "Context"]


class PiiHit(BaseModel):
    """One piece of personal data seen once, masked."""

    kind: Kind
    value_masked: str = Field(description="The value with its middle replaced by asterisks")
    source: Source
    keyframe_index: Optional[int] = None
    timestamp: Optional[float] = None
    context: str = Field(default="", description="Up to 60 characters around the hit, with every found value masked")
    box: Optional[Box] = Field(default=None, description="Where on the frame: x, y, w, h in frame pixels, when known")


# ------------------------------------------------------------------- masking


def _mask_alnum(value: str, keep_start: int = 2, keep_end: int = 2) -> str:
    """Keep the first and last letters or digits, star the rest, keep separators."""
    positions = [i for i, ch in enumerate(value) if ch.isalnum()]
    if len(positions) <= keep_start + keep_end:
        keep = set(positions[:1])
    else:
        keep = set(positions[:keep_start]) | set(positions[-keep_end:])
    return "".join(
        ch if (not ch.isalnum() or i in keep) else "*"
        for i, ch in enumerate(value)
    )


def _mask_words(value: str) -> str:
    """Keep the initial of each word: 'Priya Shah' -> 'P*** S***'; '14' -> '1*'."""
    out = []
    for word in value.split():
        if word.isdigit() or word[0].isdigit():
            out.append(word[0] + "*" * max(1, len(word) - 1))
        else:
            out.append(word[0] + "***")
    return " ".join(out)


def mask_value(kind: str, value: str) -> str:
    """The masked form of a value, by kind. Never returns the value unchanged."""
    if kind == "email":
        local, _, domain = value.partition("@")
        head = local[:2] if len(local) > 2 else local[:1]
        return f"{head}***@{domain}"
    if kind in ("person name", "address"):
        return _mask_words(value)
    return _mask_alnum(value)


# ----------------------------------------------------------------- detectors


@dataclass
class _Found:
    kind: str
    raw: str            # the value as written, only ever used to build masks and contexts
    start: int
    end: int
    extra_raw: tuple[str, ...] = ()  # other raw pieces to mask in the context (sort code + account)


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

PHONE = re.compile(
    r"""
    (?<![A-Za-z0-9])
    (?:
        \+\d{1,3}[\s-]?(?:\(0\)[\s-]?)?   # +44, +44 (0), +1
      | \(0\)[\s-]?                        # (0)20 ...
      | 0                                  # UK trunk 0
    )
    \d{1,4}(?:[\s-]?\d{2,4}){1,3}
    (?![A-Za-z0-9])
    """,
    re.X,
)

POSTCODE_STRICT = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]? \d[A-Z]{2}\b")
POSTCODE_LOOSE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}\b")
POSTCODE_WORDS = re.compile(r"post\s?code|address", re.I)

MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
DATE = re.compile(
    rf"""
    (?<!\d)
    (?:
        \d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{2,4}}                       # 04/03/1975
      | \d{{4}}-\d{{2}}-\d{{2}}                                     # 1975-03-04
      | \d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTHS}\s+\d{{4}}    # 4th of March 1975
      | {MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}            # March 4, 1975
    )
    (?!\d)
    """,
    re.X,
)
BIRTH_WORDS = re.compile(r"\bbirth\b|\bD\.?O\.?B\b|\bborn\b", re.I)

NI_NUMBER = re.compile(r"\b[A-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b")

CARD = re.compile(r"(?<![\d-])(?:\d{13,19}|\d{3,6}(?:[ -]\d{3,6}){2,5})(?![\d-])")

IBAN = re.compile(r"\bGB\d{2}\s?[A-Z]{4}(?:\s?\d){14}\b")

SORT_CODE = re.compile(r"\b\d{2}-\d{2}-\d{2}\b")
ACCOUNT_NUMBER = re.compile(r"(?<![\d-])\d{8}(?![\d-])")
SORT_ACCOUNT_REACH = 40

_NAME_VALUE = r"[A-Z][A-Za-z'\-]+(?:[ \t]+[A-Z][A-Za-z'\-]+){0,2}"
NAME_AFTER_STRONG_LABEL = re.compile(
    rf"\b(?:First name|Last name|Surname|Full name|Customer name|Applicant name|Account holder|Signed in as)"
    rf"[ \t]*[:*=]?[ \t]*({_NAME_VALUE})"
)
NAME_AFTER_WEAK_LABEL = re.compile(
    rf"\b(?:Name|Customer|Applicant)[ \t]*(?:[:=]|\*|[ \t]{{2}})[ \t]*({_NAME_VALUE})"
)
NAME_SPOKEN = re.compile(
    rf"\b(?:the\s+)?(?:customer|applicant|client|patient)\s+(?:is|was|called|named)\s+({_NAME_VALUE})"
)
NAME_STOP_WORDS = {
    "Required", "Optional", "Search", "Details", "Name", "Number", "Type", "Address", "Email",
    "Phone", "Date", "Title", "Please", "Enter", "Select", "Yes", "No", "None", "Unknown",
    "Id", "ID", "Reference", "Has", "Is", "Since", "Service", "Support", "Account", "Screen",
}

STREET_SUFFIX = r"(?:Street|Road|Gardens|Lane|Avenue|Close|Drive|Way|Place|Crescent|Square|Terrace|Court)"
ADDRESS = re.compile(
    rf"(?<![\w/.-])(\d{{1,4}}[A-Za-z]?[ \t]+(?:[A-Z][A-Za-z'\-]+[ \t]+)+{STREET_SUFFIX})\b"
)
ADDRESS_WORDS = re.compile(r"address", re.I)

OTHER_ID = re.compile(
    r"\b(?:Passport(?: no\.?| number)?|Driving licence(?: number)?|Licence number|NHS number|Customer (?:id|number|ref))"
    r"[ \t]*[:#]?[ \t]*([A-Z0-9]{2,}(?:[ -]?[A-Z0-9]{2,}){0,4})",
    re.I,
)


def luhn_ok(digits: str) -> bool:
    """True when a string of digits passes the Luhn check used by card numbers."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _find_emails(text: str) -> list[_Found]:
    return [_Found("email", m.group(0), m.start(), m.end()) for m in EMAIL.finditer(text)]


def _find_phones(text: str) -> list[_Found]:
    found = []
    for m in PHONE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if m.group(0).startswith("+"):
            ok = 9 <= len(digits) <= 15
        elif 10 <= len(digits) <= 11:
            ok = True
        else:
            # A longer number counts only when a label says it is a phone number,
            # so a 13-digit order or product code does not get flagged.
            before = text[max(0, m.start() - 40):m.start()]
            ok = 12 <= len(digits) <= 13 and bool(re.search(r"phone|mobile|tel\b", before, re.I))
        if ok:
            found.append(_Found("phone", m.group(0), m.start(), m.end()))
    return found


def _find_postcodes(text: str) -> list[_Found]:
    found = [_Found("uk postcode", m.group(0), m.start(), m.end()) for m in POSTCODE_STRICT.finditer(text)]
    for m in POSTCODE_LOOSE.finditer(text):
        if POSTCODE_WORDS.search(text[max(0, m.start() - 40): m.start()]):
            found.append(_Found("uk postcode", m.group(0), m.start(), m.end()))
    return found


def _find_dates_of_birth(text: str) -> list[_Found]:
    found = []
    for m in DATE.finditer(text):
        around = text[max(0, m.start() - 40): m.start()] + text[m.end(): m.end() + 20]
        if BIRTH_WORDS.search(around):
            found.append(_Found("date of birth", m.group(0), m.start(), m.end()))
    return found


def _find_ni_numbers(text: str) -> list[_Found]:
    return [_Found("national insurance number", m.group(0), m.start(), m.end()) for m in NI_NUMBER.finditer(text)]


def _find_cards(text: str) -> list[_Found]:
    found = []
    for m in CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            found.append(_Found("card number", m.group(0), m.start(), m.end()))
    return found


def _find_ibans(text: str) -> list[_Found]:
    return [_Found("iban", m.group(0), m.start(), m.end()) for m in IBAN.finditer(text)]


def _find_sort_code_and_account(text: str) -> list[_Found]:
    found = []
    for m in SORT_CODE.finditer(text):
        lo, hi = max(0, m.start() - SORT_ACCOUNT_REACH), m.end() + SORT_ACCOUNT_REACH
        for a in ACCOUNT_NUMBER.finditer(text, lo, hi):
            start, end = min(m.start(), a.start()), max(m.end(), a.end())
            found.append(
                _Found("sort code and account", f"{m.group(0)} / {a.group(0)}", start, end,
                       extra_raw=(m.group(0), a.group(0)))
            )
            break
    return found


def _find_names(text: str) -> list[_Found]:
    found = []
    for pattern in (NAME_AFTER_STRONG_LABEL, NAME_AFTER_WEAK_LABEL, NAME_SPOKEN):
        for m in pattern.finditer(text):
            value = re.sub(r"[ \t]+", " ", m.group(1))
            if value.split()[0] in NAME_STOP_WORDS:
                continue
            found.append(_Found("person name", value, m.start(1), m.end(1)))
    return found


def _find_addresses(text: str) -> list[_Found]:
    found = []
    for m in ADDRESS.finditer(text):
        before = text[max(0, m.start() - 40): m.start()]
        at_line_start = before.rstrip(" \t").endswith("\n") or before.strip() == ""
        if at_line_start or ADDRESS_WORDS.search(before):
            found.append(_Found("address", re.sub(r"[ \t]+", " ", m.group(1)), m.start(1), m.end(1)))
    return found


def _find_other_ids(text: str) -> list[_Found]:
    found = []
    for m in OTHER_ID.finditer(text):
        value = m.group(1)
        if re.search(r"\d", value):  # a label followed by a word ("Passport number required") is not an id
            found.append(_Found("other id", value, m.start(1), m.end(1)))
    return found


DETECTORS = [
    _find_emails,
    _find_phones,
    _find_postcodes,
    _find_dates_of_birth,
    _find_ni_numbers,
    _find_cards,
    _find_ibans,
    _find_sort_code_and_account,
    _find_names,
    _find_addresses,
    _find_other_ids,
]


# ------------------------------------------------------------------ scanning


def _detect(text: str) -> list[_Found]:
    """Every detector over one text, in reading order."""
    found: list[_Found] = []
    for detect in DETECTORS:
        found.extend(detect(text))
    found.sort(key=lambda f: (f.start, KINDS.index(f.kind)))
    return found


def _mask_pairs(found: Iterable[_Found]) -> list[tuple[str, str]]:
    """(raw, masked) for every piece of text a set of hits covers, longest raw first.

    A sort code and account number are one hit but two pieces of text, so
    each piece gets its own masked form.
    """
    pieces: list[tuple[str, str]] = []
    for f in found:
        raws = f.extra_raw or (f.raw,)
        masked = mask_value(f.kind, f.raw)
        for raw in raws:
            pieces.append((raw, masked if len(raws) == 1 else _mask_alnum(raw)))
    return sorted(pieces, key=lambda p: -len(p[0]))


def _context(text: str, found: _Found, all_found: list[_Found]) -> str:
    """Up to CONTEXT_CHARS characters around a hit, every found value masked, one line."""
    half = CONTEXT_CHARS // 2
    lo, hi = max(0, found.start - half), min(len(text), found.end + half)
    snippet = text[lo:hi]
    for raw, masked in _mask_pairs(all_found):
        snippet = snippet.replace(raw, masked)
        # A snippet may start or end part-way through a value; hide the leftover.
        for k in range(len(raw) - 1, 3, -1):
            if snippet.startswith(raw[-k:]):
                snippet = "*" * k + snippet[k:]
            if snippet.endswith(raw[:k]):
                snippet = snippet[:-k] + "*" * k
    return " ".join(snippet.split())


def scan_text(
    text: str,
    source: Source,
    keyframe_index: Optional[int] = None,
    timestamp: Optional[float] = None,
) -> list[PiiHit]:
    """Every personal-data pattern in one piece of text, masked, one hit per value."""
    if not text:
        return []
    found = _detect(text)

    hits: list[PiiHit] = []
    seen: set[tuple[str, str]] = set()
    for f in found:
        masked = mask_value(f.kind, f.raw)
        key = (f.kind, masked)
        if key in seen:
            continue
        seen.add(key)
        hits.append(
            PiiHit(
                kind=f.kind,
                value_masked=masked,
                source=source,
                keyframe_index=keyframe_index,
                timestamp=timestamp,
                context=_context(text, f, found),
            )
        )
    return hits


# ------------------------------------------------------- scanning with boxes


def union_boxes(boxes: Iterable[Box]) -> Box:
    """The smallest box that holds every box given."""
    boxes = list(boxes)
    left = min(b[0] for b in boxes)
    top = min(b[1] for b in boxes)
    right = max(b[0] + b[2] for b in boxes)
    bottom = max(b[1] + b[3] for b in boxes)
    return (left, top, right - left, bottom - top)


def _boxes_overlap(a: Box, b: Box) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


Group = tuple[list[TextLine], Optional[list[str]]]  # lines in order, and the text placed before each one


def _joined(lines: list[TextLine], separators: Optional[list[str]] = None) -> tuple[str, list[tuple[int, int]]]:
    """The lines as one text, with each line's character range.

    `separators[i]` goes before line i (a newline when not given, so the text
    reads one line per row).
    """
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = 0
    for i, line in enumerate(lines):
        sep = "\n" if i and separators is None else (separators[i] if separators else "")
        parts.append(sep)
        pos += len(sep)
        spans.append((pos, pos + len(line.text)))
        parts.append(line.text)
        pos += len(line.text)
    return "".join(parts), spans


def _rows(lines: list[TextLine]) -> list[list[TextLine]]:
    """Group boxes into rows the way `merge_rows` does, left to right within a row."""
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
    for row in rows:
        row.sort(key=lambda l: l.x)
    return rows


def _box_for_span(lines: list[TextLine], spans: list[tuple[int, int]], start: int, end: int) -> Box:
    """The union of the boxes of the lines that a character range touches."""
    boxes = [
        (line.x, line.y, line.w, line.h)
        for line, (lo, hi) in zip(lines, spans)
        if lo < end and hi > start
    ]
    return union_boxes(boxes)


@dataclass
class _Placed:
    """A detector hit on a frame, with the box of the line(s) it sat on."""

    found: _Found
    box: Box
    text: str                # the joined text the hit was found in, for the context
    all_found: list[_Found]  # every hit in that text, so the context can mask them all


def _line_groups(lines: list[TextLine]) -> list[Group]:
    """Two readings of the frame: one line per box, then boxes joined along their rows.

    In the row reading, two boxes on one row join with no gap when they sit
    close together (a value split as "priya.shah@" and "example.com") and
    with two spaces when they are apart (a label and its value), as
    `merge_rows` does. Each box keeps its own character range, so a hit's
    box covers only the boxes the value sat in.
    """
    if not lines:
        return []
    ordered = sorted(lines, key=lambda l: (l.y, l.x))
    by_row: list[TextLine] = []
    separators: list[str] = []
    for row in _rows(ordered):
        for i, line in enumerate(row):
            if i == 0:
                separators.append("\n" if by_row else "")
            else:
                prev = row[i - 1]
                close = line.x - prev.right < max(prev.h, line.h)
                separators.append("" if close else "  ")
            by_row.append(line)
    return [(ordered, None), (by_row, separators)]


def _place_in_lines(lines: list[TextLine]) -> list[_Placed]:
    """Every detector hit over the lines, each with its box.

    The lines are scanned joined one per row, so a value that runs over two
    rows (a sort code on one, the account number on the next) is found and
    takes the union of both boxes. The same value found again with an
    overlapping box keeps the tighter box.
    """
    placed: list[_Placed] = []
    for group, separators in _line_groups(lines):
        text, spans = _joined(group, separators)
        all_found = _detect(text)
        for f in all_found:
            placed.append(_Placed(f, _box_for_span(group, spans, f.start, f.end), text, all_found))
    return _dedupe_placed(placed)


def _dedupe_placed(placed: list[_Placed]) -> list[_Placed]:
    kept: list[_Placed] = []
    for p in placed:
        masked = mask_value(p.found.kind, p.found.raw)
        for i, k in enumerate(kept):
            if (k.found.kind, mask_value(k.found.kind, k.found.raw)) != (p.found.kind, masked):
                continue
            if k.box == p.box:
                break
            if _boxes_overlap(k.box, p.box):
                if p.box[2] * p.box[3] < k.box[2] * k.box[3]:
                    kept[i] = p
                break
        else:
            kept.append(p)
    kept.sort(key=lambda p: (p.box[1], p.box[0], KINDS.index(p.found.kind)))
    return kept


def scan_frame_lines(
    lines: list[TextLine],
    keyframe_index: Optional[int] = None,
    timestamp: Optional[float] = None,
) -> list[PiiHit]:
    """Every personal-data pattern in the text lines of one frame, masked, with a box each.

    Like `scan_text` over the frame's text, but each hit carries the pixel box
    of the OCR line it was found on (the union of the boxes when a value spans
    two lines). The same value in two places on the frame is two hits.
    """
    hits: list[PiiHit] = []
    for p in _place_in_lines(lines):
        hits.append(
            PiiHit(
                kind=p.found.kind,
                value_masked=mask_value(p.found.kind, p.found.raw),
                source="frame text",
                keyframe_index=keyframe_index,
                timestamp=timestamp,
                context=_context(p.text, p.found, p.all_found),
                box=p.box,
            )
        )
    return hits


def locate_hits(hits: list[PiiHit], lines_by_frame: dict[int, list[TextLine]]) -> list[PiiHit]:
    """Give frame-text hits their boxes, from the OCR lines of their frame.

    A hit only carries the masked value, so the frame's lines are scanned
    again and matched on kind and masked value. A hit found in two places
    comes back as two hits, one per box. Hits that cannot be located, and
    hits from the transcript or example values, come back unchanged.
    """
    located: list[PiiHit] = []
    by_frame: dict[int, list[PiiHit]] = {}
    for hit in hits:
        if hit.source != "frame text" or hit.box is not None or hit.keyframe_index not in lines_by_frame:
            located.append(hit)
            continue
        if hit.keyframe_index not in by_frame:
            by_frame[hit.keyframe_index] = scan_frame_lines(lines_by_frame[hit.keyframe_index], hit.keyframe_index, hit.timestamp)
        matches = [
            h for h in by_frame[hit.keyframe_index]
            if (h.kind, h.value_masked) == (hit.kind, hit.value_masked)
        ]
        if not matches:
            located.append(hit)
            continue
        for match in matches:
            located.append(hit.model_copy(update={"box": match.box}))
    return located


def _keyframe_timestamps(recording: Recording) -> dict[int, float]:
    return {kf.index: kf.timestamp for kf in recording.keyframes}


def _keyframe_for_time(recording: Recording, at: float) -> Optional[int]:
    for moment in recording.moments:
        if moment.start <= at <= moment.end:
            return moment.keyframe_index
    return None


def scan_recording(
    recording: Recording,
    analysis: Analysis,
    ocr_text: Optional[dict[int, str]] = None,
) -> list[PiiHit]:
    """Every hit in the frame text, the transcript and the field example values.

    Frame text hits take the keyframe's own timestamp; transcript hits take the
    keyframe that was showing when the segment started; example values take the
    field's frame and time. The same (kind, masked value, frame) is listed once.
    """
    times = _keyframe_timestamps(recording)
    hits: list[PiiHit] = []

    for index, text in sorted((ocr_text or {}).items()):
        hits.extend(scan_text(text, "frame text", index, times.get(index)))

    for segment in recording.segments:
        hits.extend(scan_text(segment.text, "transcript", _keyframe_for_time(recording, segment.start), segment.start))

    for field in analysis.fields:
        if field.example_value:
            # The label goes in front so a value that only counts next to its
            # label (a date of birth, a long phone number) is still recognised.
            hits.extend(scan_text(f"{field.label}: {field.example_value}", "example value",
                                  field.keyframe_index, field.timestamp))

    unique: list[PiiHit] = []
    seen: set[tuple[str, str, Optional[int]]] = set()
    for hit in hits:
        key = (hit.kind, hit.value_masked, hit.keyframe_index)
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return unique


def load_ocr_text(out_dir: Path | str) -> dict[int, str]:
    """The cached frame text from `out_dir/ocr.json`, keyframe index -> text; empty if absent."""
    path = Path(out_dir) / OCR_CACHE_NAME
    if not path.exists():
        return {}
    return {int(k): str(v) for k, v in json.loads(path.read_text()).items()}


# ------------------------------------------------------------------- output


def pii_rows(hits: list[PiiHit]) -> list[list]:
    """The sheet: a header row, then one row per hit. Frame is the bare keyframe index."""
    rows: list[list] = [list(PII_HEADERS)]
    for hit in hits:
        rows.append([
            hit.kind,
            hit.value_masked,
            hit.source,
            mmss(hit.timestamp) if hit.timestamp is not None else "",
            hit.keyframe_index if hit.keyframe_index is not None else "",
            hit.context,
        ])
    return rows


def pii_summary(hits: list[PiiHit]) -> str:
    """One sentence for the top of the sheet and the report."""
    if not hits:
        return "No personal data patterns found."
    counts = Counter(hit.kind for hit in hits)
    parts = []
    for kind in KINDS:
        n = counts.get(kind, 0)
        if n:
            singular, plural = KIND_WORDS[kind]
            parts.append(f"{n} {singular if n == 1 else plural}")
    frames = {hit.keyframe_index for hit in hits if hit.keyframe_index is not None}
    where = f" on {len(frames)} frame{'s' if len(frames) != 1 else ''}" if frames else ""
    return f"Personal data seen: {', '.join(parts)}{where}. Check before sharing."
