"""Read a transcript from a file, or make one with local speech-to-text.

Most meeting tools already export a transcript, so the file path is the common
route. Four formats are read: WebVTT, SubRip, plain text with a timestamp at the
start of each line, and the JSON that Zoom, Teams and Loom produce. All of them
come out as the same list of `TranscriptSegment`, with times in seconds.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from specto.model import TranscriptSegment

# `HH:MM:SS.mmm`, `MM:SS.mmm`, `MM:SS`, `SS.s`, with `,` or `.` before the fraction.
_TIMESTAMP_RE = re.compile(r"^(?:(\d{1,2}):)?(?:(\d{1,2}):)?(\d{1,2}(?:[.,]\d+)?)$")
_ISO_DURATION_RE = re.compile(r"^PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?$", re.I)
_CUE_TIMING_RE = re.compile(r"^\s*([\d:.,]+)\s*-->\s*([\d:.,]+)")
_VOICE_TAG_RE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]*)>")
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_PLAIN_LINE_RE = re.compile(r"^\s*\[?\s*((?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d+)?)\s*\]?\s*(?:[-–:]\s*)?(.*)$")
_SRT_SNIFF_RE = re.compile(r"^\s*\d+\s*\r?\n\s*[\d:.,]+\s*-->", re.M)


def parse_timestamp(value: Any) -> float:
    """Turn a timestamp into seconds.

    Accepts numbers (already seconds), `HH:MM:SS.mmm` / `MM:SS` strings with a dot
    or comma before the fraction, optional square brackets, and ISO `PT1M2.5S`
    durations. Raises ValueError for anything else so a bad file fails loudly.
    """
    if isinstance(value, bool):
        raise ValueError(f"not a timestamp: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"not a timestamp: {value!r}")
    text = value.strip().strip("[]").strip()
    iso = _ISO_DURATION_RE.match(text)
    if iso and text.upper().startswith("PT"):
        hours, minutes, seconds = (float(g) if g else 0.0 for g in iso.groups())
        return hours * 3600 + minutes * 60 + seconds
    match = _TIMESTAMP_RE.match(text)
    if not match:
        raise ValueError(f"not a timestamp: {value!r}")
    first, second, last = match.groups()
    seconds = float(last.replace(",", "."))
    if first is not None and second is not None:
        return int(first) * 3600 + int(second) * 60 + seconds
    if first is not None:
        return int(first) * 60 + seconds
    return seconds


def parse_transcript(path: str | Path) -> list[TranscriptSegment]:
    """Read a transcript file and return its segments in file order.

    The format is picked from the extension first, then from the content, so a
    `.txt` that is really SubRip still parses. Segments are returned exactly as
    the file has them: nothing is merged or re-timed.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    fmt = _detect_format(path, text)
    if fmt == "vtt":
        segments = _parse_vtt(text)
    elif fmt == "srt":
        segments = _parse_srt(text)
    elif fmt == "json":
        segments = _parse_json(text)
    else:
        segments = _parse_plain(text)
    print(f"transcript: {len(segments)} segments loaded from {path.name} ({fmt})")
    return segments


def transcribe(video_path: str | Path, model_size: str = "base") -> list[TranscriptSegment]:
    """Run faster-whisper on the recording's audio and return timed segments.

    faster-whisper is optional and heavy, so it is imported here rather than at
    module load. The first run downloads the model; later runs use the cache.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ImportError(
            "faster-whisper is not installed, so specto cannot transcribe the audio itself. "
            "Either `pip install faster-whisper` or pass a transcript file with --transcript."
        ) from exc

    print(f"transcript: transcribing {Path(video_path).name} with whisper '{model_size}' (this can take a while)")
    model = WhisperModel(model_size)
    raw_segments, info = model.transcribe(str(video_path), vad_filter=True)
    segments = [
        TranscriptSegment(start=float(seg.start), end=float(seg.end), text=seg.text.strip())
        for seg in raw_segments
        if seg.text.strip()
    ]
    print(f"transcript: {len(segments)} segments transcribed (language {getattr(info, 'language', '?')})")
    return segments


# ------------------------------------------------------------------ helpers


def _detect_format(path: Path, text: str) -> str:
    suffix = path.suffix.lower()
    head = text.lstrip()[:200]
    if suffix == ".vtt" or head.startswith("WEBVTT"):
        return "vtt"
    if suffix == ".json" or head.startswith(("[", "{")):
        return "json"
    if suffix == ".srt" or _SRT_SNIFF_RE.search(text):
        return "srt"
    return "plain"


def _blocks(text: str) -> list[list[str]]:
    """Split cue-style text into blocks separated by blank lines."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.strip():
            current.append(line.rstrip())
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _cue_from_block(block: list[str]) -> Optional[TranscriptSegment]:
    """Read one VTT/SRT cue block: optional id line, timing line, text lines."""
    timing_index = next((i for i, line in enumerate(block) if _CUE_TIMING_RE.match(line)), None)
    if timing_index is None:
        return None
    match = _CUE_TIMING_RE.match(block[timing_index])
    assert match is not None
    start = parse_timestamp(match.group(1))
    end = parse_timestamp(match.group(2))
    raw_text = " ".join(line.strip() for line in block[timing_index + 1 :])
    speaker = None
    voice = _VOICE_TAG_RE.search(raw_text)
    if voice:
        speaker = voice.group(1).strip() or None
    text = _ANY_TAG_RE.sub("", raw_text)
    text = re.sub(r"\s+", " ", text).strip()
    return TranscriptSegment(start=start, end=end, text=text, speaker=speaker)


def _parse_vtt(text: str) -> list[TranscriptSegment]:
    segments = []
    for block in _blocks(text):
        first = block[0].strip()
        if first.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        cue = _cue_from_block(block)
        if cue is not None:
            segments.append(cue)
    return segments


def _parse_srt(text: str) -> list[TranscriptSegment]:
    segments = []
    for block in _blocks(text):
        cue = _cue_from_block(block)
        if cue is not None:
            segments.append(cue)
    return segments


def _parse_plain(text: str) -> list[TranscriptSegment]:
    """Lines like `00:01:23 text`, `[00:01:23] text` or `1:23 text`.

    Plain text has no end times, so each segment ends where the next one starts.
    The last one gets a rough length from its word count.
    """
    found: list[tuple[float, str]] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        match = _PLAIN_LINE_RE.match(line)
        if not match:
            continue
        try:
            start = parse_timestamp(match.group(1))
        except ValueError:
            continue
        found.append((start, match.group(2).strip()))
    segments = []
    for i, (start, body) in enumerate(found):
        if i + 1 < len(found):
            end = max(start, found[i + 1][0])
        else:
            end = start + max(1.0, 0.4 * len(body.split()))
        segments.append(TranscriptSegment(start=start, end=end, text=body))
    return segments


_START_KEYS = ("start", "startTime", "start_time", "startOffset", "start_offset", "offset", "begin", "from", "timestamp", "ts", "time")
_END_KEYS = ("end", "endTime", "end_time", "endOffset", "end_offset", "to", "stop", "finish")
_DURATION_KEYS = ("duration", "dur", "length")
_TEXT_KEYS = ("text", "content", "transcript", "caption", "value", "sentence", "utterance")
_SPEAKER_KEYS = ("speaker", "speakerName", "speaker_name", "speakerLabel", "speaker_label", "name", "user", "author")


def _first_present(item: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def _find_entries(data: Any) -> list[dict]:
    """Find the list of transcript entries inside whatever the tool exported."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("segments", "entries", "results", "transcript", "items", "data", "captions", "utterances", "sentences"):
            value = data.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
            if isinstance(value, dict):
                nested = _find_entries(value)
                if nested:
                    return nested
        for value in data.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
    return []


def _parse_json(text: str) -> list[TranscriptSegment]:
    data = json.loads(text)
    segments = []
    for item in _find_entries(data):
        body = _first_present(item, _TEXT_KEYS)
        if not isinstance(body, str) or not body.strip():
            continue
        start_raw = _first_present(item, _START_KEYS)
        if start_raw is None:
            continue
        try:
            start = parse_timestamp(start_raw)
        except ValueError:
            continue
        end_raw = _first_present(item, _END_KEYS)
        duration_raw = _first_present(item, _DURATION_KEYS)
        end = start
        try:
            if end_raw is not None:
                end = parse_timestamp(end_raw)
            elif duration_raw is not None:
                end = start + parse_timestamp(duration_raw)
        except ValueError:
            pass
        speaker = _first_present(item, _SPEAKER_KEYS)
        if isinstance(speaker, dict):
            speaker = _first_present(speaker, ("name", "displayName", "label"))
        segments.append(
            TranscriptSegment(
                start=start,
                end=max(start, end),
                text=body.strip(),
                speaker=str(speaker) if speaker else None,
            )
        )
    return segments
