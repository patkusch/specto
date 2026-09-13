"""Redaction: boxes from hand-built lines without OCR, and the whole folder with the real engine."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from specto.model import Analysis, ChangedRegion, DataField, Keyframe, Question, Recording, Requirement, Screen
from specto.ocr import TextLine, ocr_available, ocr_image
from specto.pii import PiiHit, locate_hits, scan_frame_lines, scan_text, union_boxes
from specto.redact import (
    RedactReport,
    find_redactions,
    ocr_lines_for_recording,
    redact_dir,
    redact_frames,
    restore_dir,
    scrub_analysis,
)

EMAIL = "priya.shah@example.com"
PHONE = "07000 12345678"
POSTCODE = "SW1A 1AA"
ROWS = [("Email address", EMAIL), ("Phone", PHONE), ("Postcode", POSTCODE)]
ROW_Y = [100, 170, 240]
VALUE_X = 310

needs_ocr = pytest.mark.skipif(
    not ocr_available(),
    reason="OCR engine not installed or failed to load; pip install 'specto[ocr]' (rapidocr-onnxruntime)",
)


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _line(text: str, x: int, y: int, w: int = 100, h: int = 20) -> TextLine:
    return TextLine(text=text, x=x, y=y, w=w, h=h, confidence=0.9)


# ------------------------------------------------------------- without OCR


def test_scan_frame_lines_gives_each_hit_its_line_box():
    lines = [
        _line("Email address  priya.shah@example.com", 40, 100, w=500, h=24),
        _line("Phone  07000 12345678", 40, 170, w=300, h=24),
        _line("Order ORD-1001", 40, 400, w=200, h=24),
    ]
    hits = scan_frame_lines(lines, keyframe_index=3, timestamp=12.0)
    assert [(h.kind, h.value_masked, h.box) for h in hits] == [
        ("email", "pr***@example.com", (40, 100, 500, 24)),
        ("phone", "07*** ******78", (40, 170, 300, 24)),
    ]
    assert all(h.source == "frame text" and h.keyframe_index == 3 and h.timestamp == 12.0 for h in hits)
    for hit in hits:
        assert "priya.shah" not in hit.context and "345678" not in hit.context
    assert scan_frame_lines([]) == []


def test_scan_frame_lines_unions_boxes_when_a_value_spans_two_lines():
    lines = [
        _line("Sort code 12-34-56", 40, 100, w=200, h=20),
        _line("Account number 12345678", 40, 130, w=260, h=22),
    ]
    (hit,) = scan_frame_lines(lines)
    assert hit.kind == "sort code and account"
    assert hit.value_masked == "12-**-** / ******78"
    assert hit.box == (40, 100, 260, 52)
    assert "12345678" not in hit.context and "12-34-56" not in hit.context


def test_scan_frame_lines_finds_a_value_split_over_two_boxes_on_one_row():
    lines = [
        _line("Email", 40, 100, w=80, h=20),
        _line("priya.shah@", 310, 101, w=120, h=20),
        _line("example.com", 435, 101, w=110, h=20),
    ]
    (hit,) = scan_frame_lines(lines)
    assert hit.kind == "email"
    assert hit.box == (310, 101, 235, 20)   # the two value boxes, not the label


def test_scan_frame_lines_keeps_the_tighter_box_and_lists_a_repeated_value_twice():
    lines = [
        _line("Email", 40, 100, w=80, h=20),
        _line("priya.shah@example.com", 310, 100, w=250, h=20),
        _line("Contact  priya.shah@example.com", 40, 300, w=400, h=20),
    ]
    hits = scan_frame_lines(lines)
    assert [h.box for h in hits] == [(310, 100, 250, 20), (40, 300, 400, 20)]   # not the whole first row


def test_union_boxes():
    assert union_boxes([(10, 10, 20, 20), (5, 25, 10, 10)]) == (5, 10, 25, 25)
    assert union_boxes([(1, 2, 3, 4)]) == (1, 2, 3, 4)


def test_locate_hits_adds_boxes_to_frame_text_hits_only():
    lines = {1: [_line("Email  priya.shah@example.com", 40, 100, w=400, h=24)]}
    hits = scan_text("Email  priya.shah@example.com", "frame text", keyframe_index=1, timestamp=45.0)
    transcript = PiiHit(kind="email", value_masked="pr***@example.com", source="transcript", keyframe_index=1)
    unknown = PiiHit(kind="phone", value_masked="07*** ******78", source="frame text", keyframe_index=1)
    located = locate_hits(hits + [transcript, unknown], lines)
    assert [(h.source, h.box) for h in located] == [
        ("frame text", (40, 100, 400, 24)),
        ("transcript", None),
        ("frame text", None),
    ]
    assert located[0].timestamp == 45.0 and located[0].context == hits[0].context


def test_pii_hit_box_round_trips_through_json():
    hit = PiiHit(kind="email", value_masked="pr***@example.com", source="frame text", box=(1, 2, 3, 4))
    assert PiiHit.model_validate_json(hit.model_dump_json()).box == (1, 2, 3, 4)
    assert PiiHit(kind="email", value_masked="x***@y.co", source="transcript").box is None


def test_redact_frames_box_style_paints_and_keeps_the_original(tmp_path: Path):
    recording = _recording(tmp_path)
    hits = [PiiHit(kind="email", value_masked="pr***@example.com", source="frame text", keyframe_index=0, box=(300, 100, 460, 40))]
    before = (tmp_path / "frames/frame_0000.jpg").read_bytes()

    painted = redact_frames(recording, tmp_path, hits, log=lambda _: None)

    assert painted == 1
    assert (tmp_path / "frames/original/frame_0000.jpg").read_bytes() == before
    with Image.open(tmp_path / "frames/frame_0000.jpg") as img:
        assert max(img.getpixel((530, 120))) < 60
        assert min(img.getpixel((530, 300))) > 200   # untouched elsewhere
    assert not (tmp_path / "frames/crop_0000.jpg").exists()   # first frame: no change region


def test_redact_frames_blur_style_hides_the_text(tmp_path: Path):
    recording = _recording(tmp_path)
    hits = [PiiHit(kind="email", value_masked="pr***@example.com", source="frame text", keyframe_index=0, box=(300, 100, 460, 40))]
    with Image.open(tmp_path / "frames/frame_0000.jpg") as img:
        crisp = img.crop((300, 100, 760, 140)).convert("L")
    redact_frames(recording, tmp_path, hits, style="blur", log=lambda _: None)
    with Image.open(tmp_path / "frames/frame_0000.jpg") as img:
        soft = img.crop((300, 100, 760, 140)).convert("L")
    # Crisp text has many dark pixels; a heavy blur leaves none.
    assert sum(1 for v in crisp.tobytes() if v < 100) > 200
    assert sum(1 for v in soft.tobytes() if v < 100) == 0


def test_redact_frames_regenerates_the_crop_from_the_redacted_frame(tmp_path: Path):
    recording = _recording(tmp_path, frames=2)
    recording.keyframes[1].change_from_previous = ChangedRegion(x=300, y=90, w=470, h=60, fraction=0.03)
    crop = tmp_path / "frames/crop_0001.jpg"
    crop.write_bytes(b"stale")
    hits = [PiiHit(kind="email", value_masked="pr***@example.com", source="frame text", keyframe_index=1, box=(300, 100, 460, 40))]

    redact_frames(recording, tmp_path, hits, log=lambda _: None)

    assert crop.read_bytes() != b"stale"
    with Image.open(crop) as img:
        w, h = img.size
        assert max(img.getpixel((w // 2, h // 2))) < 60


def test_scrub_analysis_masks_only_values_it_located(tmp_path: Path):
    recording = _recording(tmp_path)
    analysis = _analysis()
    lines = {0: [_line("Email address  priya.shah@example.com", 40, 100, w=500, h=24)]}
    hits = find_redactions(recording, analysis, lines)
    assert [(h.source, h.box) for h in hits] == [("frame text", (40, 100, 500, 24))]

    scrubbed, count = scrub_analysis(analysis, hits)

    assert count == 3
    assert scrubbed.fields[0].example_value == "pr***@example.com"
    assert scrubbed.requirements[0].source_quote == "the email pr***@example.com is checked"
    assert scrubbed.questions[0].question == "Is pr***@example.com always the primary contact?"
    assert scrubbed.questions[0].context_quote == "phone 07000 12345678 was not read off the frame"   # not located
    assert analysis.fields[0].example_value == EMAIL   # the input is not changed in place
    assert scrub_analysis(analysis, [])[1] == 0


def test_ocr_lines_cache_round_trips_and_missing_engine_is_a_clear_error(tmp_path: Path, monkeypatch):
    from specto import redact as redact_module

    recording = _recording(tmp_path)
    (tmp_path / "ocr_lines.json").write_text(json.dumps({"0": [_line("Phone 07000 12345678", 40, 170).model_dump()]}))
    lines = ocr_lines_for_recording(recording, tmp_path, log=lambda _: None)
    assert lines == {0: [_line("Phone 07000 12345678", 40, 170)]}

    (tmp_path / "ocr_lines.json").unlink()
    monkeypatch.setattr(redact_module, "ocr_available", lambda: False)
    with pytest.raises(RuntimeError, match=r'pip install "specto\[ocr\]"'):
        ocr_lines_for_recording(recording, tmp_path, log=lambda _: None)


def test_restore_dir_without_originals_is_a_no_op(tmp_path: Path):
    assert restore_dir(tmp_path) == 0


# -------------------------------------------------------------- fixtures


def render_form(path: Path) -> Path:
    """A 1280x720 fake screen with three labelled values."""
    img = Image.new("RGB", (1280, 720), (245, 245, 245))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 1280, 56], fill=(30, 50, 90))
    draw.text((24, 14), "ACME Portal - Customer Details", fill="white", font=_font(26))
    for (label, value), y in zip(ROWS, ROW_Y):
        draw.text((40, y + 6), label, fill=(20, 20, 20), font=_font(24))
        draw.rectangle([300, y, 760, y + 40], fill="white", outline=(120, 120, 120))
        draw.text((VALUE_X, y + 6), value, fill=(20, 20, 20), font=_font(24))
    draw.rectangle([40, 360, 320, 404], fill=(0, 110, 60))
    draw.text((60, 370), "Submit for approval", fill="white", font=_font(22))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, quality=92)
    return path


def _recording(out_dir: Path, frames: int = 1) -> Recording:
    keyframes = []
    for i in range(frames):
        rel = f"frames/frame_{i:04d}.jpg"
        render_form(out_dir / rel)
        keyframes.append(Keyframe(index=i, timestamp=10.0 * i, path=rel, width=1280, height=720))
    return Recording(source="form.mp4", duration=10.0 * frames, keyframes=keyframes)


def _analysis() -> Analysis:
    return Analysis(
        title="Customer details",
        summary="The officer checks the customer's contact details.",
        screens=[Screen(id="S01", name="Customer Details", purpose="Check contact details", keyframe_indexes=[0], first_seen=0.0, field_ids=["F001"])],
        fields=[DataField(id="F001", screen_id="S01", label="Email address", field_type="text", example_value=EMAIL, source="seen on screen", timestamp=0.0, keyframe_index=0)],
        requirements=[Requirement(id="R001", statement="The system must show the email address.", source_quote=f"the email {EMAIL} is checked", timestamp=0.0, keyframe_index=0, confidence="high")],
        questions=[Question(id="Q001", question=f"Is {EMAIL} always the primary contact?", why_it_matters="Contact rules", context_quote=f"phone {PHONE} was not read off the frame", timestamp=0.0, keyframe_index=0)],
    )


def _dark_at(path: Path, x: int, y: int) -> bool:
    with Image.open(path) as img:
        return max(img.getpixel((x, y))) < 60


# ---------------------------------------------------------------- real engine


@needs_ocr
def test_redact_dir_paints_scrubs_and_restores(tmp_path: Path):
    recording = _recording(tmp_path)
    (tmp_path / "recording.json").write_text(recording.model_dump_json(indent=2))
    (tmp_path / "analysis.json").write_text(_analysis().model_dump_json(indent=2))
    frame = tmp_path / "frames/frame_0000.jpg"
    before = frame.read_bytes()
    logged: list[str] = []

    report = redact_dir(tmp_path, style="box", log=logged.append)

    assert isinstance(report, RedactReport)
    assert report.frames_touched == 1
    assert report.boxes_painted >= 3
    assert report.kinds.get("email", 0) >= 1 and report.kinds.get("phone", 0) >= 1 and report.kinds.get("uk postcode", 0) >= 1
    assert report.text_replacements >= 3

    assert (tmp_path / "frames/original/frame_0000.jpg").read_bytes() == before
    assert frame.read_bytes() != before
    for y in ROW_Y:
        assert _dark_at(frame, VALUE_X + 60, y + 18), f"value row at y={y} is not painted over"
    assert not _dark_at(frame, 640, 600)   # the empty part of the page is untouched

    analysis = Analysis.model_validate_json((tmp_path / "analysis.json").read_text())
    assert analysis.fields[0].example_value == "pr***@example.com"
    assert "priya" not in (tmp_path / "analysis.json").read_text()

    ocr_text = json.loads((tmp_path / "ocr.json").read_text())
    assert "priya" not in ocr_text["0"].lower()
    assert "345678" not in ocr_text["0"] and "1AA" not in ocr_text["0"]
    redacted_text = " ".join(line.text for line in ocr_image(frame)).lower()
    assert "priya" not in redacted_text and "example.com" not in redacted_text

    # No hit carries the raw value, and the line cache holds only what OCR read.
    hits = find_redactions(recording, _analysis(), ocr_lines_for_recording(recording, tmp_path, log=lambda _: None))
    assert all(EMAIL not in h.model_dump_json() for h in hits)
    assert any("frames/original" in line for line in logged)

    # A second run paints the same spots again and changes nothing else.
    second = redact_dir(tmp_path, style="box", log=lambda _: None)
    assert second.boxes_painted == report.boxes_painted
    assert (tmp_path / "frames/original/frame_0000.jpg").read_bytes() == before

    assert restore_dir(tmp_path) == 1
    assert frame.read_bytes() == before
    assert "priya" in json.loads((tmp_path / "ocr.json").read_text())["0"]


@needs_ocr
def test_find_redactions_locates_an_example_value_by_search(tmp_path: Path):
    recording = _recording(tmp_path)
    lines = ocr_lines_for_recording(recording, tmp_path, log=lambda _: None)
    analysis = _analysis()
    analysis.fields[0].example_value = PHONE   # the engine reads it without the space
    analysis.fields[0].label = "Phone number"   # a long number counts as a phone only next to a phone label

    hits = find_redactions(recording, analysis, lines)

    phones = [h for h in hits if h.kind == "phone"]
    assert phones and all(h.box is not None for h in phones)
    box = phones[0].box
    assert box[1] <= ROW_Y[1] + 20 <= box[1] + box[3]
    assert len(phones) == 1   # the frame scan and the example-value search agree on one spot
    scrubbed, count = scrub_analysis(analysis, hits)
    assert count >= 1 and scrubbed.fields[0].example_value == "07*** ******78"
