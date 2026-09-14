"""Cheap checks on the standing example in examples/onboarding.

These only read files that are committed. Rebuilding the recording (Playwright,
`say`) and transcribing it (whisper model download) are deliberately not done
here; see examples/onboarding/README.md for how that was measured by hand.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from specto.transcript import parse_transcript

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "onboarding"
MP4 = EXAMPLE / "walkthrough.mp4"
VTT = EXAMPLE / "walkthrough.vtt"
EXPECTED = EXAMPLE / "expected.json"
DETAILS = EXAMPLE / "app" / "details.html"


def load_make_example():
    """Import examples/onboarding/make_example.py as a module (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("make_example", EXAMPLE / "make_example.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


# ------------------------------------------------------------ sample customer


@pytest.fixture(scope="module")
def make_example():
    return load_make_example()


def test_sample_customer_is_deterministic(make_example):
    assert make_example.sample_customer() == make_example.sample_customer()
    assert make_example.sample_customer(seed=1) == make_example.sample_customer(seed=1)
    assert make_example.sample_customer(seed=1) != make_example.sample_customer(seed=2)


def test_sample_customer_values_look_like_placeholders(make_example):
    customer = make_example.sample_customer()
    match = re.fullmatch(r"01/01/(\d{4})", customer["date_of_birth"])
    assert match and 1960 <= int(match.group(1)) <= 1999
    assert re.fullmatch(r"07700 900\d{3}", customer["phone"])       # Ofcom's range for fiction
    assert customer["email"].endswith("@example.com")
    assert customer["email"] == f"{customer['first_name'].lower()}.{customer['last_name'].lower()}@example.com"
    assert customer["postcode"] == "SW1A 1AA"                         # spoken in the narration, must not move
    assert re.fullmatch(r"\d{1,2} \S.*", customer["address_line_1"])
    assert customer["address_line_2"] == ""
    for row in customer["matches"]:
        assert row["name"].endswith(" " + customer["last_name"])      # the search was by surname
        assert row["date_of_birth"].startswith("01/01/")
    names = [customer["signed_in_user"], customer["team_lead"], customer["colleague"]] + [r["name"] for r in customer["queue"]]
    assert len(set(names)) == len(names)


def test_details_page_holds_exactly_the_generated_values(make_example):
    customer = make_example.sample_customer()
    page = DETAILS.read_text(encoding="utf-8")
    values = re.findall(r'<input[^>]*value="([^"]*)"', page)
    assert values == [
        customer["first_name"], customer["last_name"], customer["date_of_birth"], customer["email"], customer["phone"],
        customer["address_line_1"], customer["address_line_2"], customer["town"], customer["postcode"],
    ]
    assert f"Signed in as <b>{customer['signed_in_user']}</b>" in page


def test_committed_pages_match_their_templates(make_example, tmp_path, monkeypatch):
    """app/*.html is what make_example.py writes; an edit to a page must go through the template."""
    monkeypatch.setattr(make_example, "APP", tmp_path)
    rendered = make_example.render_pages()
    assert sorted(rendered) == sorted(p.name for p in (EXAMPLE / "app").glob("*.html"))
    for name, page in rendered.items():
        assert page == (EXAMPLE / "app" / name).read_text(encoding="utf-8"), f"{name} differs from its template"
        assert "{{" not in page
