"""Tests for the optional frame OCR.

Row merging and the cache are tested without the engine. The real-engine test
skips with a reason when the `ocr` extra is not installed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from specto import ocr as ocr_module
from specto.model import Keyframe, Recording
from specto.ocr import TextLine, merge_rows, ocr_available, ocr_image, ocr_recording


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow: no size argument
        return ImageFont.load_default()


def render_form(path: Path) -> Path:
    """A 1280x720 fake screen: form labels and values, a button, a small table."""
    img = Image.new("RGB", (1280, 720), (245, 245, 245))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 1280, 56], fill=(30, 50, 90))
    draw.text((24, 14), "ACME Portal - Customer Details", fill="white", font=_font(26))
    rows = [
        ("Customer name", "Jane Example"),
        ("Date of birth", "12/03/1980"),
        ("Postcode *", "SW1A 1AA"),
        ("Email address", "jane@example.com"),
    ]
    for i, (label, value) in enumerate(rows):
        y = 100 + i * 60
        draw.text((40, y + 6), label, fill=(20, 20, 20), font=_font(22))
        draw.rectangle([300, y, 700, y + 36], fill="white", outline=(120, 120, 120))
        draw.text((310, y + 6), value, fill=(20, 20, 20), font=_font(22))
    draw.rectangle([40, 360, 320, 404], fill=(0, 110, 60))
    draw.text((60, 370), "Submit for approval", fill="white", font=_font(22))
    headers = ["Order ID", "Status", "Amount", "Created"]
    for j, header in enumerate(headers):
        x = 40 + j * 250
        draw.rectangle([x, 460, x + 240, 496], fill=(220, 220, 220), outline=(120, 120, 120))
        draw.text((x + 10, 468), header, fill=(20, 20, 20), font=_font(20))
    data = [("ORD-1001", "Pending", "120.00", "2026-09-01"), ("ORD-1002", "Approved", "85.50", "2026-09-03")]
    for i, row in enumerate(data):
        for j, cell in enumerate(row):
            x, y = 40 + j * 250, 500 + i * 36
            draw.rectangle([x, y, x + 240, y + 34], fill="white", outline=(160, 160, 160))
            draw.text((x + 10, y + 7), cell, fill=(20, 20, 20), font=_font(18))
    img.save(path)
    return path


# ------------------------------------------------------------------ no engine


def _line(text: str, x: int, y: int, w: int = 100, h: int = 20, confidence: float = 0.9) -> TextLine:
    return TextLine(text=text, x=x, y=y, w=w, h=h, confidence=confidence)


def test_merge_rows_joins_same_row_left_to_right_and_orders_rows() -> None:
    raw = [
        _line("SW1A 1AA", x=310, y=231),       # value, slightly lower than its label
        _line("Postcode *", x=40, y=229),
        _line("12/03/1980", x=310, y=170),
        _line("Date of birth", x=40, y=168),
        _line("Submit for approval", x=60, y=373, w=200, h=25),
        _line("Amount", x=550, y=470),
        _line("Order ID", x=48, y=469),
        _line("Status", x=298, y=470),
    ]
    merged = merge_rows(raw)
    assert [m.text for m in merged] == [
        "Date of birth  12/03/1980",
        "Postcode *  SW1A 1AA",
        "Submit for approval",
        "Order ID  Status  Amount",
    ]
    first = merged[0]
    assert (first.x, first.y) == (40, 168)
    assert first.w == 310 + 100 - 40
    assert first.h == 170 + 20 - 168
    assert first.confidence == pytest.approx(0.9)
    assert merge_rows([]) == []


def test_merge_rows_keeps_stacked_rows_apart() -> None:
    raw = [_line("row two", x=10, y=40), _line("row one", x=10, y=10)]
    assert [m.text for m in merge_rows(raw)] == ["row one", "row two"]


def _tiny_recording(out_dir: Path, count: int = 3) -> Recording:
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    keyframes = []
    for i in range(count):
        rel = f"frames/frame_{i:04d}.png"
        Image.new("RGB", (64, 48), (200, 200, 200)).save(out_dir / rel)
        keyframes.append(Keyframe(index=i, timestamp=5.0 * i, path=rel, width=64, height=48))
    return Recording(source="tiny.mp4", duration=15.0, keyframes=keyframes)


def test_ocr_recording_writes_cache_and_reloads_without_rerunning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = _tiny_recording(tmp_path)
    calls: list[Path] = []

    def fake_ocr_image(path):
        calls.append(Path(path))
        n = int(Path(path).stem.split("_")[-1])
        if n == 1:
            return []  # a frame with no text at all
        return [_line("Postcode *  SW1A 1AA", 40, 100), _line(f"frame {n}", 40, 130)]

    monkeypatch.setattr(ocr_module, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr_module, "ocr_image", fake_ocr_image)
    logged: list[str] = []

    first = ocr_recording(recording, tmp_path, log=logged.append)

    assert len(calls) == 3
    assert first == {0: "Postcode *  SW1A 1AA\nframe 0", 1: "", 2: "Postcode *  SW1A 1AA\nframe 2"}
    cache = tmp_path / "ocr.json"
    assert cache.exists()
    raw = json.loads(cache.read_text())
    assert list(raw) == ["0", "1", "2"]  # string keys
    assert raw["2"] == "Postcode *  SW1A 1AA\nframe 2"
    assert any("3 frames read, 2 with text" in line for line in logged)

    second = ocr_recording(recording, tmp_path, log=logged.append)
    assert len(calls) == 3  # nothing re-run
    assert second == first
    assert all(isinstance(k, int) for k in second)

    ocr_recording(recording, tmp_path, force=True, log=logged.append)
    assert len(calls) == 6


def test_ocr_recording_skips_with_a_note_when_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = _tiny_recording(tmp_path)
    monkeypatch.setattr(ocr_module, "ocr_available", lambda: False)
    monkeypatch.setattr(ocr_module, "ocr_image", lambda path: pytest.fail("engine must not be called"))
    logged: list[str] = []

    assert ocr_recording(recording, tmp_path, log=logged.append) == {}

    assert not (tmp_path / "ocr.json").exists()
    assert len(logged) == 1
    assert "not installed" in logged[0]


def test_ocr_image_raises_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ocr_image(tmp_path / "nope.png")


# ---------------------------------------------------------------- real engine


@pytest.mark.skipif(
    not ocr_available(),
    reason="OCR engine not installed or failed to load; pip install 'specto[ocr]' (rapidocr-onnxruntime)",
)
def test_ocr_image_reads_a_rendered_form(tmp_path: Path) -> None:
    path = render_form(tmp_path / "form.png")

    lines = ocr_image(path)

    assert lines, "the engine found no text at all"
    all_text = "\n".join(line.text for line in lines).lower()
    assert "postcode" in all_text
    assert "submit" in all_text
    assert "order id" in all_text
    # Row merging keeps a label and its value together on one line.
    dob = [line for line in lines if "date of birth" in line.text.lower()]
    assert len(dob) == 1
    assert "1980" in dob[0].text
    # Lines come top to bottom with sensible pixel boxes and confidences.
    assert [line.y for line in lines] == sorted(line.y for line in lines)
    for line in lines:
        assert 0 <= line.x < 1280 and 0 <= line.y < 720
        assert line.w > 0 and line.h > 0
        assert 0.0 <= line.confidence <= 1.0
