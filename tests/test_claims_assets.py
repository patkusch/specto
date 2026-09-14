"""Cheap checks on the standing example in examples/claims.

These only read files that are committed, plus the pure parts of the build
script (the seeded sample data and the meeting-frame compositor). Rebuilding
the recording (Playwright, `say`) is deliberately not done here; see
examples/claims/README.md for how to do that by hand.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from PIL import Image

from specto.transcript import parse_transcript

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "claims"
MP4 = EXAMPLE / "walkthrough.mp4"
VTT = EXAMPLE / "walkthrough.vtt"
EXPECTED = EXAMPLE / "expected.json"


@pytest.fixture(scope="module")
def make_example():
    """The build script as a module, loaded from its path (it is not a package)."""
    spec = importlib.util.spec_from_file_location("claims_make_example", EXAMPLE / "make_example.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recording_files_exist():
    assert MP4.is_file(), f"missing {MP4}"
    assert VTT.is_file(), f"missing {VTT}"
    assert 0 < MP4.stat().st_size < 6_000_000, "mp4 should exist and stay under 6 MB"


def test_vtt_parses_into_narration_lines():
    segments = parse_transcript(VTT)
    assert len(segments) >= 14
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
    assert len(expected["screens"]) == 6


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


def test_sample_data_is_deterministic(make_example):
    first = make_example.sample_data()
    second = make_example.sample_data()
    assert first == second
    assert make_example.sample_data(seed=1) != first, "a different seed should give different values"
    rows = first["rows"]
    assert len(rows) == 8
    for row in rows:
        assert row["ref"].startswith("CMP-2026-0") and len(row["ref"]) == len("CMP-2026-01234")
        assert row["received"].startswith("01/"), "dates are the first of a month"
        assert len(row["customer"].split(" ")) == 2
    assert first["complaint"]["phone"].startswith("07700 900")
    assert first["redress"]["amount"] in (50, 100, 150, 200, 250, 300, 400, 500, 750, 1000)


def test_committed_pages_match_the_generator(make_example):
    """The HTML in app/ is what the seeded generator writes; a stale page would show here."""
    data = make_example.sample_data()
    for index, (_, file, _) in enumerate(make_example.SCRIPT):
        committed = (EXAMPLE / "app" / file).read_text(encoding="utf-8")
        assert committed == make_example.page_html(index, data), f"{file} differs from the generator output"


def test_meeting_frame_places_the_page_at_160_60(make_example):
    page_colour = (250, 20, 130)  # a colour the frame never uses
    page = Image.new("RGB", (1280, 720), page_colour)
    canvas = make_example.compose_meeting_frame(page, "01:23", mic_on=True)
    assert canvas.size == (1600, 900)
    px = canvas.load()
    x, y = make_example.WINDOW_X, make_example.WINDOW_Y
    assert (x, y) == (160, 60)
    for inside in [(x, y), (x + 1279, y), (x, y + 719), (x + 1279, y + 719), (x + 640, y + 360)]:
        assert px[inside] == page_colour, f"pixel {inside} should be the page"
    for outside in [(x - 1, y), (x + 1280, y), (x, y - 1), (x, y + 720), (10, 450), (1590, 450), (800, 890)]:
        assert px[outside] != page_colour, f"pixel {outside} should be the frame, not the page"


def test_meeting_frame_mic_dot_and_timer_are_small(make_example):
    """The blinking dot and the timer must stay under the crop detector's 2% rule
    (32 px along a row, 18 px down a column on a 1600x900 canvas)."""
    page = Image.new("RGB", (1280, 720), (255, 255, 255))
    on = make_example.compose_meeting_frame(page, "00:00", mic_on=True).convert("L")
    off = make_example.compose_meeting_frame(page, "09:59", mic_on=False).convert("L")
    width, height = on.size
    changed = [(i % width, i // width) for i, (a, b) in enumerate(zip(on.tobytes(), off.tobytes())) if abs(a - b) > 24]
    assert changed, "the dot and the timer should differ between the two frames"
    rows: dict[int, int] = {}
    cols: dict[int, int] = {}
    for cx, cy in changed:
        rows[cy] = rows.get(cy, 0) + 1
        cols[cx] = cols.get(cx, 0) + 1
    assert max(rows.values()) < 0.02 * width
    assert max(cols.values()) < 0.02 * height
