"""Cheap checks on the held-out example in examples/deliveries.

These only read files that are committed, plus the pure parts of the build
script (the seeded sample data and the page generator). Rebuilding the
recording (Playwright, `say`) is deliberately not done here; see
examples/deliveries/README.md for how to do that by hand.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg
import pytest

from specto.transcript import parse_transcript

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "deliveries"
MP4 = EXAMPLE / "walkthrough.mp4"
VTT = EXAMPLE / "walkthrough.vtt"
EXPECTED = EXAMPLE / "expected.json"


@pytest.fixture(scope="module")
def make_example():
    """The build script as a module, loaded from its path (it is not a package)."""
    spec = importlib.util.spec_from_file_location("deliveries_make_example", EXAMPLE / "make_example.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _video_size(path: Path) -> tuple[int, int]:
    stderr = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                            capture_output=True, text=True, errors="replace").stderr
    line = next(l for l in stderr.splitlines() if "Video:" in l)
    match = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line.split("Video:", 1)[1])
    assert match, f"no size in {line!r}"
    return int(match.group(1)), int(match.group(2))


def test_recording_files_exist():
    assert MP4.is_file(), f"missing {MP4}"
    assert VTT.is_file(), f"missing {VTT}"
    assert 0 < MP4.stat().st_size < 6_000_000, "mp4 should exist and stay under 6 MB"


def test_recording_is_portrait():
    width, height = _video_size(MP4)
    assert height > width, f"expected a portrait recording, got {width}x{height}"
    assert (width, height) == (540, 1170)


def test_vtt_parses_into_narration_lines():
    segments = parse_transcript(VTT)
    assert 14 <= len(segments) <= 18
    assert all(seg.end > seg.start for seg in segments)
    assert all(seg.speaker == "Expert" for seg in segments)
    starts = [seg.start for seg in segments]
    assert starts == sorted(starts)


def test_narration_says_what_the_answer_key_relies_on(make_example):
    text = " ".join(line for _, _, lines in make_example.SCRIPT for line in lines).lower()
    assert make_example.VOICE not in ("Samantha", "Daniel")
    for rule in ("over one hundred pounds need a photo and a signature", "three failed attempts",
                 "only supervisors can reassign", "stays grey until the photo is taken"):
        assert rule in text, f"narration lost the rule {rule!r}"
    assert "never worked out why" in text and "somewhere in finance" in text
    assert "every stop has a phone number" in text
    assert "hazard" not in text, "the Hazardous badge must never be mentioned aloud"


@pytest.fixture(scope="module")
def expected() -> dict:
    return json.loads(EXPECTED.read_text(encoding="utf-8"))


def test_expected_has_all_sections(expected):
    for key in ("screens", "fields", "actions", "requirements", "questions"):
        assert key in expected, f"expected.json is missing {key!r}"
        assert expected[key], f"expected.json has an empty {key!r}"
    assert len(expected["screens"]) == 6
    assert len(expected["actions"]) == 4
    assert 6 <= len(expected["requirements"]) <= 7
    assert len(expected["questions"]) == 5


def test_expected_was_hand_written_not_generated_from_a_model():
    raw = EXPECTED.read_text(encoding="utf-8").lower()
    assert '"confidence"' not in raw, "a field named confidence means the key came out of a model reading"
    for value in json.loads(raw).values():
        if isinstance(value, list):
            for item in value:
                assert not (isinstance(item, dict) and "confidence" in item)


def test_expected_fields_point_at_known_screens(expected):
    screens = [name.lower() for name in expected["screens"]]
    for field in expected["fields"]:
        assert any(field["screen"].lower() in name or name in field["screen"].lower() for name in screens), field
        assert field["label"].strip()


def test_expected_groups_are_lists_of_strings(expected):
    for key in ("actions", "requirements", "questions"):
        for item in expected[key]:
            assert isinstance(item, list) and item, f"{key} item {item!r} should be a non-empty list of groups"
            assert all(isinstance(group, str) and group for group in item)


def test_sample_data_is_deterministic(make_example):
    first = make_example.sample_data()
    assert first == make_example.sample_data()
    assert make_example.sample_data(seed=1) != first, "a different seed should give different values"
    assert make_example.SEED == 20260918
    assert len(first["stops"]) == 6
    for stop in first["stops"]:
        assert stop["phone"] is None or stop["phone"].startswith("07700 900")
        for parcel in stop["parcels"]:
            assert re.fullmatch(r"HL-\d+", parcel["id"])
            assert parcel["value"] % 10 == 0, "amounts are round"
        assert stop["address"].split(" ", 1)[1] in make_example._STREETS
    assert first["date"].startswith("01/") and first["previous_attempt"].startswith("01/"), "dates are the first of a month"
    assert first["summary"]["cash"] % 10 == 0
    # The seeded story the answer key depends on.
    detail = first["stops"][make_example.DETAIL_STOP]
    assert detail["phone"] is None, "one stop must have no phone number"
    assert detail["parcels"][0]["value"] > 100 and any(p["hazardous"] for p in detail["parcels"])


def test_committed_pages_match_the_generator(make_example):
    """The HTML and CSS in app/ are what the seeded generator writes; a stale page would show here."""
    data = make_example.sample_data()
    for index, (_, file, _) in enumerate(make_example.SCRIPT):
        committed = (EXAMPLE / "app" / file).read_text(encoding="utf-8")
        assert committed == make_example.page_html(index, data), f"{file} differs from the generator output"
    assert (EXAMPLE / "app" / "style.css").read_text(encoding="utf-8") == make_example.STYLE


def test_pages_show_the_screen_only_facts(make_example):
    data = make_example.sample_data()
    stop_page = make_example.page_html(1, data)
    assert "Hazardous" in stop_page and "No number on file" in stop_page
    assert 'class="btn off">Call' in stop_page
    assert 'class="btn off block">Delivered' in make_example.page_html(2, data)
