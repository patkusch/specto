"""Personal-data scan: each detector on a hit and a near-miss, masking, the example page, and the sheet."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest

from specto.model import Analysis, Recording
from specto.pii import (
    PII_HEADERS,
    PiiHit,
    load_ocr_text,
    luhn_ok,
    mask_value,
    pii_rows,
    pii_summary,
    scan_recording,
    scan_text,
)

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE_PAGE = Path(__file__).resolve().parent.parent / "examples" / "onboarding" / "app" / "details.html"


def kinds(text: str) -> list[str]:
    return [h.kind for h in scan_text(text, "frame text")]


def masked(text: str, kind: str) -> list[str]:
    return [h.value_masked for h in scan_text(text, "frame text") if h.kind == kind]


# ---------------------------------------------------------------- detectors


def test_email_hit_and_near_miss():
    assert masked("Email address priya.shah@example.com", "email") == ["pr***@example.com"]
    assert "email" not in kinds("Contact us at example.com or @support")


@pytest.mark.parametrize("phone", ["07700 900123", "+44 7700 900123", "+44 (0)20 7946 0958", "020-7946-0958"])
def test_phone_forms(phone):
    assert "phone" in kinds(f"Phone number {phone}")


def test_phone_near_misses():
    assert "phone" not in kinds("Product code PC07700900123X")   # letters glued on: a product code
    assert "phone" not in kinds("Help: ext. 4410")               # too short
    assert "phone" not in kinds("Northwind Onboarding v4.2")


def test_postcode_strict_and_near_miss():
    assert masked("SW1A 1AA", "uk postcode") == ["SW** *AA"]
    assert "uk postcode" not in kinds("SW1A")                    # outward half alone
    assert "uk postcode" not in kinds("Ref EC1A1BB")             # no space, no label nearby
    assert masked("Postcode: EC1A1BB", "uk postcode") == ["EC***BB"]  # accepted because the label says postcode


def test_date_of_birth_needs_a_birth_word():
    assert masked("Date of birth * 14/06/1988", "date of birth") == ["14/**/**88"]
    assert "date of birth" in kinds("DOB 1988-06-14")
    assert "date of birth" in kinds("she was born on the 14th of June 1988")
    assert "date of birth" not in kinds("Signed on 14/06/2020")
    assert "date of birth" not in kinds("Application date 12/03/1988")


def test_national_insurance_number():
    assert masked("NI number QQ 12 34 56 C", "national insurance number") == ["QQ ** ** *6 C"]
    assert "national insurance number" in kinds("NINO QQ123456C")
    assert "national insurance number" not in kinds("QQ 12 34 5 C")


def test_card_number_must_pass_luhn():
    assert luhn_ok("4111111111111111")
    assert not luhn_ok("4111111111111112")
    assert masked("Card 4111 1111 1111 1111", "card number") == ["41** **** **** **11"]
    assert "card number" in kinds("Card 4111111111111111")
    assert "card number" not in kinds("Order 4111 1111 1111 1112")   # fails Luhn: an order number


def test_iban():
    assert masked("IBAN GB29 NWBK 6016 1331 9268 19", "iban") == ["GB** **** **** **** **** 19"]
    assert "iban" in kinds("GB29NWBK60161331926819")
    assert "iban" not in kinds("GB29 NWBK 6016")


def test_sort_code_and_account():
    hits = scan_text("Sort code 12-34-56 Account number 12345678", "frame text")
    pair = [h for h in hits if h.kind == "sort code and account"]
    assert [h.value_masked for h in pair] == ["12-**-** / ******78"]   # the pair is masked as one value
    assert "12345678" not in pair[0].context and "12-34-56" not in pair[0].context
    assert "sort code and account" not in kinds("Sort code 12-34-56 with no account nearby")
    assert "sort code and account" not in kinds("Account number 12345678 alone")


def test_person_name_only_after_a_label_or_spoken_form():
    assert masked("First name * Priya", "person name") == ["P***"]
    assert masked("Signed in as Jo Patel · Onboarding Officer", "person name") == ["J*** P***"]
    assert masked("Name: Priya Shah", "person name") == ["P*** S***"]
    assert masked("the customer is Priya Shah and she", "person name") == ["P*** S***"]
    assert "person name" not in kinds("Customer Details")        # a screen heading, not a person
    assert "person name" not in kinds("Priya Shah")              # no label: no general name recognition
    assert "person name" not in kinds("First name * Required")


def test_address():
    assert masked("14 Pembridge Gardens\nLondon", "address") == ["1* P*** G***"]
    assert "address" in kinds("Address line 1  221B Baker Street")
    assert "address" not in kinds("Step 14 Pembridge Gardens")   # mid-sentence, no label
    assert "address" not in kinds("14 pembridge gardens")        # not capitalised


def test_other_id():
    assert masked("Passport number 123456789", "other id") == ["12*****89"]
    assert "other id" not in kinds("Passport number required")


# ------------------------------------------------------------------ masking


def test_masking_rules():
    assert mask_value("email", "priya.shah@example.com") == "pr***@example.com"
    assert mask_value("email", "a@example.com") == "a***@example.com"
    assert mask_value("phone", "07700 900123") == "07*** ****23"
    assert mask_value("person name", "Priya Shah") == "P*** S***"
    assert mask_value("uk postcode", "SW1A 1AA") == "SW** *AA"
    assert mask_value("other id", "AB12") == "A***"                # short values keep one character only


def test_hit_never_carries_the_raw_value():
    text = "Email priya.shah@example.com Phone 07700 900123"
    for hit in scan_text(text, "frame text"):
        assert "priya.shah" not in hit.context and "900123" not in hit.context
        assert "priya.shah" not in hit.value_masked
        assert not hasattr(hit, "value")


def test_context_is_short_and_one_line():
    text = "x" * 200 + "\n\nEmail:\npriya.shah@example.com\n" + "y" * 200
    (hit,) = scan_text(text, "frame text")
    assert len(hit.context) <= 60 + len("pr***@example.com")
    assert "\n" not in hit.context
    assert "pr***@example.com" in hit.context


# ------------------------------------------------------------- example page


def page_text(path: Path) -> str:
    """The page as OCR would see it: labels followed by the values typed in the boxes."""
    raw = path.read_text()
    raw = re.sub(r'<input[^>]*value="([^"]*)"[^>]*>', r" \1 ", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def test_example_details_page():
    hits = scan_text(page_text(EXAMPLE_PAGE), "frame text", keyframe_index=1, timestamp=45.0)
    found = {(h.kind, h.value_masked) for h in hits}
    assert ("email", "pr***@example.com") in found
    assert ("phone", "07*** ****23") in found
    assert ("uk postcode", "SW** *AA") in found
    assert ("date of birth", "14/**/**88") in found
    assert ("person name", "P***") in found and ("person name", "S***") in found
    joined = " ".join(h.value_masked + " " + h.context for h in hits)
    for raw in ("priya.shah", "900123", "SW1A 1AA", "14/06/1988", "Priya", "Shah"):
        assert raw not in joined


# ---------------------------------------------------------- scan_recording


@pytest.fixture
def recording() -> Recording:
    return Recording.model_validate(json.loads((FIXTURES / "sample_recording.json").read_text()))


@pytest.fixture
def analysis() -> Analysis:
    return Analysis.model_validate(json.loads((FIXTURES / "sample_analysis.json").read_text()))


def test_scan_recording_sources_and_frames(recording, analysis):
    ocr = {1: "First name  Jane\nEmail  jane.doe@example.com\nPhone  07700 900123"}
    hits = scan_recording(recording, analysis, ocr)

    frame_hits = [h for h in hits if h.source == "frame text"]
    assert {h.keyframe_index for h in frame_hits} == {1}
    assert all(h.timestamp == 45.0 for h in frame_hits)           # keyframe 1's own time
    assert ("phone", "07*** ****23") in {(h.kind, h.value_masked) for h in frame_hits}

    example_hits = [h for h in hits if h.source == "example value"]
    assert ("uk postcode", "SW** *AA", 0) in {(h.kind, h.value_masked, h.keyframe_index) for h in example_hits}
    assert ("date of birth", "12/**/**88") not in {(h.kind, h.value_masked) for h in example_hits}  # bare value, no label
    # F007's email is also in the frame text for frame 1, so it is listed once, as frame text.
    emails = [h for h in hits if h.kind == "email" and h.keyframe_index == 1]
    assert [(h.value_masked, h.source) for h in emails] == [("ja***@example.com", "frame text")]

    without_ocr = scan_recording(recording, analysis, None)
    assert ("email", "ja***@example.com", 1, "example value", 58.0) in {
        (h.kind, h.value_masked, h.keyframe_index, h.source, h.timestamp) for h in without_ocr
    }


def test_scan_recording_transcript_takes_the_moment_frame(recording, analysis):
    recording.segments[0].text = "The customer is Priya Shah, her email is priya.shah@example.com."
    hits = [h for h in scan_recording(recording, analysis, None) if h.source == "transcript"]
    assert {(h.kind, h.value_masked) for h in hits} == {("person name", "P*** S***"), ("email", "pr***@example.com")}
    assert all(h.keyframe_index == 0 and h.timestamp == 2.0 for h in hits)


def test_scan_recording_dedupes_same_value_on_same_frame(recording, analysis):
    ocr = {1: "Email  jane.doe@example.com\nContact  jane.doe@example.com"}
    hits = scan_recording(recording, analysis, ocr)
    emails = [h for h in hits if h.kind == "email" and h.value_masked == "ja***@example.com" and h.keyframe_index == 1]
    assert len(emails) == 1      # once in the frame text and once as F007's example value, listed once
    ocr = {1: "Email  jane.doe@example.com", 2: "Email  jane.doe@example.com"}
    hits = scan_recording(recording, analysis, ocr)
    assert len([h for h in hits if h.kind == "email"]) == 2   # a different frame is a separate row


def test_load_ocr_text(tmp_path):
    assert load_ocr_text(tmp_path) == {}
    (tmp_path / "ocr.json").write_text(json.dumps({"3": "Email x@y.co", "0": ""}))
    assert load_ocr_text(tmp_path) == {3: "Email x@y.co", 0: ""}


# ---------------------------------------------------------------- the sheet


def test_pii_rows():
    hits = [
        PiiHit(kind="email", value_masked="pr***@example.com", source="frame text", keyframe_index=4, timestamp=125.0, context="Email pr***@example.com"),
        PiiHit(kind="person name", value_masked="P*** S***", source="transcript", keyframe_index=None, timestamp=None),
    ]
    rows = pii_rows(hits)
    assert rows[0] == PII_HEADERS == ["Kind", "Value (masked)", "Where", "Time", "Frame", "Context"]
    assert rows[1] == ["email", "pr***@example.com", "frame text", "02:05", 4, "Email pr***@example.com"]
    assert rows[2] == ["person name", "P*** S***", "transcript", "", "", ""]


def test_pii_summary_wording():
    assert pii_summary([]) == "No personal data patterns found."

    def hit(kind, value, frame):
        return PiiHit(kind=kind, value_masked=value, source="frame text", keyframe_index=frame)

    hits = [
        hit("email", "a***@x.com", 1), hit("email", "b***@x.com", 2), hit("email", "c***@x.com", 2),
        hit("phone", "07*** ****23", 3), hit("phone", "07*** ****24", 4),
        hit("uk postcode", "SW** *AA", 1),
    ]
    assert pii_summary(hits) == "Personal data seen: 3 emails, 2 phone numbers, 1 postcode on 4 frames. Check before sharing."
    assert pii_summary([hit("person name", "P***", 0)]) == "Personal data seen: 1 name on 1 frame. Check before sharing."
