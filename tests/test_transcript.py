"""Transcript parser tests, one per format, plus the timestamp helper.

`transcribe()` is only checked for its import error message: no whisper model
is downloaded in tests.
"""
from __future__ import annotations

import builtins
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from specto.transcript import _segment_from_whisper, parse_timestamp, parse_transcript, transcribe

VTT_WITH_VOICES = """WEBVTT - Zoom style export
Kind: captions
Language: en

NOTE
This block is a comment and must be skipped.
Even across two lines.

STYLE
::cue { color: white }

1
00:00:01.000 --> 00:00:03.500
<v Patti Kusch>Hello, this is the <b>customer</b> search screen.</v>

00:03.600 --> 00:05.000
<v.loud Expert>Type the postcode here.

NOTE another comment

00:01:02.250 --> 00:01:04.000
No voice tag on this one.
"""

SRT_MULTILINE = """1
00:00:00,500 --> 00:00:02,000
First cue, one line.

2
00:00:02,500 --> 00:00:06,750
Second cue spans
two lines of text.

3
01:02:03,000 --> 01:02:04,000
<i>Third</i> after an hour.
"""

PLAIN_MIXED = """Transcript of the walkthrough
00:00:05 Open the search screen.
[00:00:12] Type the postcode and press Find.
1:03 - Now press Save.
01:02:03.5 Last line with an hour and a half second.
this line has no timestamp and is ignored
"""


def test_parse_timestamp_variants():
    assert parse_timestamp("00:01:23") == 83.0
    assert parse_timestamp("00:01:23.500") == 83.5
    assert parse_timestamp("00:01:23,500") == 83.5
    assert parse_timestamp("1:23") == 83.0
    assert parse_timestamp("[1:23]") == 83.0
    assert parse_timestamp("12.5") == 12.5
    assert parse_timestamp(12) == 12.0
    assert parse_timestamp("PT1M2.5S") == 62.5
    with pytest.raises(ValueError):
        parse_timestamp("yesterday")


def test_parse_vtt_with_voice_tags_and_notes(tmp_path: Path):
    path = tmp_path / "talk.vtt"
    path.write_text(VTT_WITH_VOICES, encoding="utf-8")
    segments = parse_transcript(path)
    assert len(segments) == 3
    first, second, third = segments
    assert (first.start, first.end) == (1.0, 3.5)
    assert first.text == "Hello, this is the customer search screen."
    assert first.speaker == "Patti Kusch"
    assert (second.start, second.end) == (3.6, 5.0)
    assert second.text == "Type the postcode here."
    assert second.speaker == "Expert"
    assert third.start == 62.25
    assert third.speaker is None
    assert third.text == "No voice tag on this one."


def test_parse_srt_multiline(tmp_path: Path):
    path = tmp_path / "talk.srt"
    path.write_text(SRT_MULTILINE, encoding="utf-8")
    segments = parse_transcript(path)
    assert [s.text for s in segments] == [
        "First cue, one line.",
        "Second cue spans two lines of text.",
        "Third after an hour.",
    ]
    assert (segments[0].start, segments[0].end) == (0.5, 2.0)
    assert (segments[1].start, segments[1].end) == (2.5, 6.75)
    assert segments[2].start == 3723.0
    assert all(s.speaker is None for s in segments)


def test_srt_content_in_txt_file_is_detected(tmp_path: Path):
    path = tmp_path / "zoom.txt"
    path.write_text(SRT_MULTILINE, encoding="utf-8")
    assert len(parse_transcript(path)) == 3


def test_parse_plain_text_timestamps(tmp_path: Path):
    path = tmp_path / "notes.txt"
    path.write_text(PLAIN_MIXED, encoding="utf-8")
    segments = parse_transcript(path)
    assert [s.start for s in segments] == [5.0, 12.0, 63.0, 3723.5]
    assert [s.text for s in segments] == [
        "Open the search screen.",
        "Type the postcode and press Find.",
        "Now press Save.",
        "Last line with an hour and a half second.",
    ]
    assert [s.end for s in segments[:3]] == [12.0, 63.0, 3723.5]
    assert segments[3].end > segments[3].start


@pytest.mark.parametrize(
    "entries",
    [
        [{"start": 1.0, "end": 2.5, "text": "one"}, {"start": 3, "end": 4, "text": "two", "speaker": "Ann"}],
        [{"startTime": "00:00:01.000", "endTime": "00:00:02.500", "text": "one"}, {"startTime": 3, "endTime": 4, "text": "two", "speakerName": "Ann"}],
        [{"offset": 1.0, "duration": 1.5, "text": "one"}, {"offset": 3, "duration": 1, "text": "two", "speaker": {"name": "Ann"}}],
    ],
    ids=["start-end", "startTime-endTime", "offset-duration"],
)
def test_parse_json_shapes(tmp_path: Path, entries):
    path = tmp_path / "export.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    segments = parse_transcript(path)
    assert [(s.start, s.end, s.text) for s in segments] == [(1.0, 2.5, "one"), (3.0, 4.0, "two")]
    assert segments[0].speaker is None
    assert segments[1].speaker == "Ann"


def test_parse_json_wrapped_in_object_and_tolerant_of_junk(tmp_path: Path):
    data = {
        "meeting": "Walkthrough",
        "transcript": {
            "segments": [
                {"start": 0, "end": 1, "text": "kept"},
                {"start": "not a time", "end": 1, "text": "dropped, bad start"},
                {"start": 2, "end": 3, "text": ""},
                {"start": 4, "text": "no end, fine"},
                "not even a dict",
            ]
        },
    }
    path = tmp_path / "teams.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    segments = parse_transcript(path)
    assert [s.text for s in segments] == ["kept", "no end, fine"]
    assert segments[1].end == 4.0


def test_transcribe_explains_missing_dependency(tmp_path: Path, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("faster_whisper"):
            raise ImportError("pretend it is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "faster_whisper", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="--transcript"):
        transcribe(tmp_path / "anything.mp4")


def test_segment_from_whisper_strips_word_spaces_and_tolerates_no_words():
    """The shape both faster-whisper and stable-ts hand back, with no model run."""
    raw = [SimpleNamespace(start=0.5, end=0.9, word=" So"), SimpleNamespace(start=0.9, end=1.2, word=" this"), SimpleNamespace(start=1.2, end=1.2, word="  ")]
    seg = _segment_from_whisper(0.5, 1.2, " So this", raw)
    assert seg.text == "So this"
    assert [(w.start, w.end, w.text) for w in seg.words] == [(0.5, 0.9, "So"), (0.9, 1.2, "this")]
    assert _segment_from_whisper(2.0, 3.0, " later ", None).words == []


def test_file_parsers_leave_words_empty(tmp_path: Path):
    path = tmp_path / "talk.vtt"
    path.write_text(VTT_WITH_VOICES, encoding="utf-8")
    assert all(s.words == [] for s in parse_transcript(path))
