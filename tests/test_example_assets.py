"""Cheap checks on the standing example in examples/onboarding.

These only read files that are committed. Rebuilding the recording (Playwright,
`say`) and transcribing it (whisper model download) are deliberately not done
here; see examples/onboarding/README.md for how that was measured by hand.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from specto.transcript import parse_transcript

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "onboarding"
MP4 = EXAMPLE / "walkthrough.mp4"
VTT = EXAMPLE / "walkthrough.vtt"
EXPECTED = EXAMPLE / "expected.json"


def test_recording_files_exist():
    assert MP4.is_file(), f"missing {MP4}"
    assert VTT.is_file(), f"missing {VTT}"
    assert 0 < MP4.stat().st_size < 6_000_000, "mp4 should exist and stay under 6 MB"


def test_vtt_parses_into_narration_lines():
    segments = parse_transcript(VTT)
    assert len(segments) >= 12
    assert all(seg.end > seg.start for seg in segments)
    assert all(seg.speaker == "Expert" for seg in segments)
    starts = [seg.start for seg in segments]
    assert starts == sorted(starts)


@pytest.fixture(scope="module")
def expected() -> dict:
    return json.loads(EXPECTED.read_text(encoding="utf-8"))


def test_expected_has_all_sections(expected):
    for key in ("screens", "fields", "actions", "requirements", "questions"):
        assert key in expected, f"expected.json is missing {key!r}"
        assert expected[key], f"expected.json has an empty {key!r}"


def test_expected_fields_point_at_known_screens(expected):
    screens = [name.lower() for name in expected["screens"]]
    for field in expected["fields"]:
        assert field["screen"].lower() in screens, f"field {field['label']!r} names an unknown screen {field['screen']!r}"
        assert field["label"].strip()


def test_expected_groups_are_lists_of_strings(expected):
    for key in ("actions", "requirements", "questions"):
        for item in expected[key]:
            assert isinstance(item, list) and item, f"{key} item {item!r} should be a non-empty list of groups"
            assert all(isinstance(group, str) and group for group in item)
