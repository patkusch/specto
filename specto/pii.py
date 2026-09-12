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
from typing import Literal, Optional

from pydantic import BaseModel, Field

from specto.model import Analysis, Recording
from specto.ocr import OCR_CACHE_NAME
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
        \d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{2,4}}                       # 14/06/1988
      | \d{{4}}-\d{{2}}-\d{{2}}                                     # 1988-06-14
      | \d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTHS}\s+\d{{4}}    # 14th of June 1988
      | {MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}            # June 14, 1988
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
        else:
            ok = 10 <= len(digits) <= 11
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


def _context(text: str, found: _Found, all_found: list[_Found]) -> str:
    """Up to CONTEXT_CHARS characters around a hit, every found value masked, one line."""
    half = CONTEXT_CHARS // 2
    lo, hi = max(0, found.start - half), min(len(text), found.end + half)
    snippet = text[lo:hi]
    pieces: list[tuple[str, str]] = []
    for f in all_found:
        raws = f.extra_raw or (f.raw,)
        masked = mask_value(f.kind, f.raw)
        for raw in raws:
            pieces.append((raw, masked if len(raws) == 1 else _mask_alnum(raw)))
    for raw, masked in sorted(pieces, key=lambda p: -len(p[0])):
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
    found: list[_Found] = []
    for detect in DETECTORS:
        found.extend(detect(text))
    found.sort(key=lambda f: (f.start, KINDS.index(f.kind)))

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
            hits.extend(scan_text(field.example_value, "example value", field.keyframe_index, field.timestamp))

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
