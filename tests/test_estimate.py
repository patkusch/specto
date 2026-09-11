"""Tests for the no-model cost estimate."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from specto.estimate import (
    COMPARED_MODELS,
    Estimate,
    compare_models,
    estimate,
    format_comparison,
    format_estimate,
)
from specto.model import Keyframe, Moment, Recording, TranscriptSegment

FRAME_COUNT = 20
FRAME_TOKENS = round(1280 * 720 / 750)  # 1229


def make_recording(tmp_path: Path, frames: int = FRAME_COUNT, with_jpegs: bool = False, stored_size: bool = True) -> Recording:
    """A recording with `frames` keyframes of 1280x720, one moment each, and a sentence per frame."""
    (tmp_path / "frames").mkdir(exist_ok=True)
    keyframes, moments, segments = [], [], []
    for i in range(frames):
        path = f"frames/frame_{i:04d}.jpg"
        if with_jpegs:
            Image.new("RGB", (1280, 720), (240, 240, 240)).save(tmp_path / path, "JPEG")
        keyframes.append(
            Keyframe(index=i, timestamp=float(i * 6), path=path,
                     width=1280 if stored_size else None, height=720 if stored_size else None)
        )
        segment = TranscriptSegment(start=i * 6.0, end=i * 6.0 + 5, text=f"On frame {i} we type the postcode and press Save.")
        segments.append(segment)
        moments.append(Moment(keyframe_index=i, start=i * 6.0, end=i * 6.0 + 6, segments=[segment]))
    return Recording(source="walkthrough.mp4", duration=frames * 6.0, keyframes=keyframes, segments=segments, moments=moments)


def test_calls_and_image_tokens(tmp_path: Path) -> None:
    est = estimate(make_recording(tmp_path), tmp_path, frames_per_call=8)
    assert est.calls == 4  # 3 chunks of 8, 8, 4 plus one consolidation
    assert est.images == FRAME_COUNT
    assert est.image_tokens == FRAME_COUNT * FRAME_TOKENS
    assert est.text_tokens > 0
    assert est.cached_tokens > 0  # the read prompt is cached after the first chunk
    assert est.output_tokens == 3 * 1500 + 4000
    assert est.total_cost_usd > 0
    assert est.total_cost_usd == pytest.approx(est.input_cost_usd + est.output_cost_usd)
    assert est.notes == []


def test_frames_per_call_changes_calls(tmp_path: Path) -> None:
    rec = make_recording(tmp_path)
    assert estimate(rec, tmp_path, frames_per_call=4).calls == 6
    assert estimate(rec, tmp_path, frames_per_call=20).calls == 2


def test_models_ordered_by_price(tmp_path: Path) -> None:
    rec = make_recording(tmp_path)
    costs = {m: estimate(rec, tmp_path, model=m).total_cost_usd for m in COMPARED_MODELS}
    assert costs["claude-haiku-4-5"] < costs["claude-sonnet-5"] < costs["claude-opus-5"] < costs["claude-fable-5-1"]
    # same run, so the token counts do not depend on the model
    per_model = [estimate(rec, tmp_path, model=m) for m in COMPARED_MODELS]
    assert len({(e.image_tokens, e.text_tokens, e.output_tokens) for e in per_model}) == 1


def test_ocr_text_raises_text_tokens(tmp_path: Path) -> None:
    rec = make_recording(tmp_path)
    without = estimate(rec, tmp_path)
    ocr = {i: "Customer name  Jane Example\nPostcode  SW1A 1AA\n" * 5 for i in range(FRAME_COUNT)}
    with_ocr = estimate(rec, tmp_path, ocr_text=ocr)
    assert with_ocr.text_tokens > without.text_tokens
    assert with_ocr.image_tokens == without.image_tokens
    assert with_ocr.total_cost_usd > without.total_cost_usd


def test_ocr_text_is_capped_per_frame(tmp_path: Path) -> None:
    rec = make_recording(tmp_path)
    short = estimate(rec, tmp_path, ocr_text={0: "x" * 1500})
    long = estimate(rec, tmp_path, ocr_text={0: "x" * 50_000})
    assert long.text_tokens == short.text_tokens


def test_crops_are_counted_when_present(tmp_path: Path) -> None:
    rec = make_recording(tmp_path)
    plain = estimate(rec, tmp_path)
    Image.new("RGB", (600, 300), "white").save(tmp_path / "frames" / "crop_0003.jpg", "JPEG")
    Image.new("RGB", (600, 300), "white").save(tmp_path / "frames" / "crop_0010.jpg", "JPEG")
    with_crops = estimate(rec, tmp_path)
    assert with_crops.images == plain.images + 2
    assert with_crops.image_tokens == plain.image_tokens + 2 * round(600 * 300 / 750)
    assert with_crops.calls == plain.calls


def test_size_falls_back_to_the_jpeg_then_to_default(tmp_path: Path) -> None:
    with_jpegs = make_recording(tmp_path, frames=4, with_jpegs=True, stored_size=False)
    est = estimate(with_jpegs, tmp_path)
    assert est.image_tokens == 4 * FRAME_TOKENS
    assert est.notes == []

    other = tmp_path / "other"
    other.mkdir()
    no_files = make_recording(other, frames=4, with_jpegs=False, stored_size=False)
    est = estimate(no_files, other)
    assert est.image_tokens == 4 * FRAME_TOKENS  # the 1280x720 default
    assert any("assumed" in note for note in est.notes)


def test_unknown_model_uses_opus_prices_and_notes_it(tmp_path: Path) -> None:
    rec = make_recording(tmp_path)
    unknown = estimate(rec, tmp_path, model="claude-something-9")
    opus = estimate(rec, tmp_path, model="claude-opus-5")
    assert unknown.total_cost_usd == pytest.approx(opus.total_cost_usd)
    assert unknown.model == "claude-something-9"
    assert any("claude-something-9" in note and "claude-opus-5" in note for note in unknown.notes)
    assert "Note:" in format_estimate(unknown)


def test_empty_recording(tmp_path: Path) -> None:
    est = estimate(Recording(source="x.mp4", duration=0.0), tmp_path)
    assert est.calls == 0
    assert est.total_cost_usd == 0
    text = format_estimate(est)
    assert "0 model calls" in text
    assert "per hour" not in text


def test_no_moments_falls_back_to_keyframes(tmp_path: Path) -> None:
    rec = make_recording(tmp_path, frames=9)
    rec.moments = []
    est = estimate(rec, tmp_path, frames_per_call=8)
    assert est.calls == 3
    assert est.images == 9
    assert any("no moments" in note for note in est.notes)


def test_format_estimate_lines(tmp_path: Path) -> None:
    est = estimate(make_recording(tmp_path), tmp_path)
    text = format_estimate(est)
    lines = text.splitlines()
    assert 3 <= len(lines) <= 4
    assert lines[0].startswith("Estimated cost on claude-opus-5: $")
    assert "4 model calls" in lines[0]
    assert "20 images" in lines[0]
    assert "per hour of recording" in lines[1]
    assert "02:00 long" in lines[1]  # 20 frames * 6 s
    assert "Estimates are rough" in lines[2]
    assert est.cost_per_hour_usd == pytest.approx(est.total_cost_usd * 30)


def test_compare_models(tmp_path: Path) -> None:
    rec = make_recording(tmp_path)
    estimates = compare_models(rec, tmp_path)
    assert [e.model for e in estimates] == list(COMPARED_MODELS)
    assert all(isinstance(e, Estimate) for e in estimates)
    costs = [e.total_cost_usd for e in estimates]
    assert costs == sorted(costs)
    text = format_comparison(estimates)
    for model in COMPARED_MODELS:
        assert model in text
    assert "per hour of recording" in text
    assert format_comparison([]) == "Nothing to compare."
