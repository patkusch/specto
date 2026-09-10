"""Stage 1 tests: duration, keyframes, alignment and the ingest wrapper.

Everything runs offline against the synthetic videos from conftest.py.
"""
from __future__ import annotations

import json
from pathlib import Path

from specto.align import build_moments
import pytest

from specto.ingest import content_hash, dhash, extract_keyframes, hamming, ingest, probe_duration
from specto.model import Keyframe, Recording, TranscriptSegment

VTT = """WEBVTT

00:00.000 --> 00:02.500
<v Expert>This is the customer search screen.</v>

00:03.200 --> 00:05.500
<v Expert>Now the customer details with all the fields.</v>

00:06.400 --> 00:08.500
<v Expert>Then it lands in the approval queue.</v>

00:09.500 --> 00:11.500
<v Expert>And we are done.</v>
"""


def test_probe_duration(synthetic_video: Path):
    duration = probe_duration(synthetic_video)
    assert 11.0 <= duration <= 13.0


def test_extract_keyframes_finds_each_screen(synthetic_video: Path, tmp_path: Path):
    frames = extract_keyframes(synthetic_video, tmp_path, max_width=500)
    assert 3 <= len(frames) <= 6
    timestamps = [f.timestamp for f in frames]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == len(timestamps)
    assert abs(frames[0].timestamp) < 0.6
    for screen_start in (0.0, 3.0, 6.0, 9.0):  # one frame at the start of every screen
        assert any(abs(t - screen_start) < 0.6 for t in timestamps), f"no keyframe near {screen_start}s: {timestamps}"
    for i, frame in enumerate(frames):
        assert frame.index == i
        assert frame.path == f"frames/frame_{i:04d}.jpg"
        assert (tmp_path / frame.path).exists()
        assert frame.phash and len(frame.phash) == 16
        assert frame.width is not None and frame.width <= 500
        assert frame.height is not None and frame.height > 0
    assert sorted(p.name for p in (tmp_path / "frames").iterdir()) == [f"frame_{i:04d}.jpg" for i in range(len(frames))]


def test_extract_keyframes_respects_max_frames(synthetic_video: Path, tmp_path: Path):
    frames = extract_keyframes(synthetic_video, tmp_path, max_frames=2)
    assert len(frames) == 2
    assert abs(frames[0].timestamp) < 0.6
    assert frames[1].timestamp > frames[0].timestamp


def test_extract_keyframes_min_gap_drops_close_frames(synthetic_video: Path, tmp_path: Path):
    # Screens change at 3, 6 and 9 s. With a 4 s gap, 3 s is too close to 0, 9 s too close to 6.
    frames = extract_keyframes(synthetic_video, tmp_path / "scene", detect="scene", min_gap=4.0)
    assert [round(f.timestamp) for f in frames] == [0, 6]
    # Hash mode compares each sample with the last kept frame, so the screen that
    # changed at 3 s is still picked up, one sample later, once the gap allows it.
    frames = extract_keyframes(synthetic_video, tmp_path / "hash", detect="hash", min_gap=4.0)
    assert [round(f.timestamp) for f in frames] == [0, 4, 8]


def test_extract_keyframes_removes_near_duplicates(tmp_path: Path, capsys):
    """A small widget toggling on an otherwise unchanged screen is not a new screen."""
    from PIL import ImageDraw

    from conftest import FPS, draw_screen, encode_frames

    png = tmp_path / "png"
    png.mkdir()
    plain = draw_screen("Customer Details", (245, 245, 245), 5)
    toggled = plain.copy()
    ImageDraw.Draw(toggled).rectangle([520, 60, 600, 140], fill=(0, 0, 0))  # ~3% of the picture
    other = draw_screen("Approval Queue", (30, 120, 60), 3)
    n = 0
    for img in (plain, toggled, other):
        for _ in range(FPS * 3):
            img.save(png / f"img_{n:04d}.png")
            n += 1
    video = encode_frames(png, tmp_path / "toggle.mp4")

    frames = extract_keyframes(video, tmp_path / "out", detect="scene", scene_threshold=0.005)
    out = capsys.readouterr().out
    assert "scene detection found 3 frames" in out
    assert "removed 1 near-duplicates" in out
    assert [round(f.timestamp) for f in frames] == [0, 6]
    assert sorted(p.name for p in (tmp_path / "out" / "frames").iterdir()) == ["frame_0000.jpg", "frame_0001.jpg"]


@pytest.mark.parametrize("detect", ["hash", "scene"])
def test_static_video_falls_back_to_sampling(static_video: Path, tmp_path: Path, detect: str, capsys):
    frames = extract_keyframes(static_video, tmp_path, detect=detect, fallback_interval=5)
    assert "static screen, sampled" in capsys.readouterr().out
    assert len(frames) >= 4
    assert abs(frames[0].timestamp) < 0.6
    gaps = [b.timestamp - a.timestamp for a, b in zip(frames, frames[1:])]
    assert all(4.0 <= g <= 6.0 for g in gaps)
    assert all((tmp_path / f.path).exists() for f in frames)


def test_dhash_is_stable_and_discriminates(tmp_path: Path):
    from PIL import Image, ImageDraw

    a = Image.new("RGB", (200, 100), (255, 255, 255))
    ImageDraw.Draw(a).rectangle([10, 10, 100, 90], fill=(0, 0, 0))
    b = a.copy()
    ImageDraw.Draw(b).rectangle([50, 10, 200, 90], fill=(0, 0, 0))
    a.save(tmp_path / "a.jpg")
    a.resize((100, 50)).save(tmp_path / "a_small.jpg")
    b.save(tmp_path / "b.jpg")
    assert hamming(dhash(tmp_path / "a.jpg"), dhash(tmp_path / "a_small.jpg")) <= 6
    assert hamming(dhash(tmp_path / "a.jpg"), dhash(tmp_path / "b.jpg")) > 6


def _screen_starts_found(frames, starts, tolerance=0.6):
    return [s for s in starts if any(abs(f.timestamp - s) < tolerance for f in frames)]


def test_hash_mode_sees_colour_only_change(colour_only_video: Path, tmp_path: Path, capsys):
    """Same layout in green, red, then blue: hash mode finds all three, the brightness detector none."""
    hashed = extract_keyframes(colour_only_video, tmp_path / "hash", detect="hash")
    assert _screen_starts_found(hashed, [0.0, 3.0, 6.0]) == [0.0, 3.0, 6.0], [f.timestamp for f in hashed]
    assert len(hashed) == 3

    capsys.readouterr()
    scene = extract_keyframes(colour_only_video, tmp_path / "scene", detect="scene")
    out = capsys.readouterr().out
    assert "scene detection found 1 frames" in out  # only frame 0, which is always kept
    assert len(_screen_starts_found(scene, [3.0, 6.0])) < 2  # the contrast the hash detector exists for


def test_hash_mode_sees_text_typed_into_form(typed_form_video: Path, tmp_path: Path, capsys):
    """An empty form, then the same form with three values typed, then another screen."""
    hashed = extract_keyframes(typed_form_video, tmp_path / "hash", detect="hash")
    assert _screen_starts_found(hashed, [0.0, 3.0, 6.0]) == [0.0, 3.0, 6.0], [f.timestamp for f in hashed]
    assert len(hashed) == 3

    capsys.readouterr()
    scene = extract_keyframes(typed_form_video, tmp_path / "scene", detect="scene")
    out = capsys.readouterr().out
    assert "scene detection found 2 frames" in out  # frame 0 and the jump to the green queue at 6 s
    assert 3.0 not in _screen_starts_found(scene, [3.0])  # the typed form is missed


def test_both_modes_agree_on_four_screens(synthetic_video: Path, tmp_path: Path):
    hashed = extract_keyframes(synthetic_video, tmp_path / "hash", detect="hash")
    scene = extract_keyframes(synthetic_video, tmp_path / "scene", detect="scene")
    assert [f.timestamp for f in hashed] == [f.timestamp for f in scene] == [0.0, 3.0, 6.0, 9.0]
    assert [(f.index, f.path, f.width, f.height) for f in hashed] == [(f.index, f.path, f.width, f.height) for f in scene]


def test_hash_mode_timestamps_follow_the_sample_grid(synthetic_video: Path, tmp_path: Path, capsys):
    frames = extract_keyframes(synthetic_video, tmp_path, detect="hash", sample_fps=2.0, min_gap=0.0)
    out = capsys.readouterr().out
    assert "looked at 24 samples (2 per second) and kept 4" in out
    timestamps = [f.timestamp for f in frames]
    assert timestamps == sorted(timestamps)
    assert timestamps[0] == 0.0
    assert all(abs(t * 2 - round(t * 2)) < 1e-9 for t in timestamps), timestamps  # multiples of 0.5 s
    assert timestamps == [0.0, 3.0, 6.0, 9.0]
    for i, frame in enumerate(frames):
        assert frame.index == i and frame.path == f"frames/frame_{i:04d}.jpg"
        assert (tmp_path / frame.path).exists()


def test_hash_mode_deletes_samples_it_did_not_keep(synthetic_video: Path, tmp_path: Path):
    frames = extract_keyframes(synthetic_video, tmp_path, detect="hash", sample_fps=1.0)
    names = sorted(p.name for p in (tmp_path / "frames").iterdir())
    assert names == [f"frame_{i:04d}.jpg" for i in range(len(frames))]  # 12 samples in, 4 files left
    assert not list(tmp_path.glob("**/sample_*.jpg"))
    # The same holds when max_frames thins the kept ones further.
    frames = extract_keyframes(synthetic_video, tmp_path, detect="hash", max_frames=2)
    assert [f.timestamp for f in frames] == [0.0, 9.0]
    assert sorted(p.name for p in (tmp_path / "frames").iterdir()) == ["frame_0000.jpg", "frame_0001.jpg"]


def test_hash_distance_can_be_raised_to_ignore_small_changes(typed_form_video: Path, tmp_path: Path, capsys):
    """A high threshold turns the typed values back into 'same screen'; the queue page still counts."""
    extract_keyframes(typed_form_video, tmp_path, detect="hash", hash_distance=64)
    out = capsys.readouterr().out
    assert "looked at 9 samples (1 per second) and kept 2 (distance over 64)" in out
    assert "static screen" in out  # two screens is below the three that count as a moving recording


def test_unknown_detector_is_refused(synthetic_video: Path, tmp_path: Path):
    with pytest.raises(ValueError, match="detect must be"):
        extract_keyframes(synthetic_video, tmp_path, detect="magic")


def test_content_hash_sees_colour_and_text_but_not_noise(tmp_path: Path):
    from conftest import draw_screen, form_screen

    green = draw_screen("Approval Queue", (60, 160, 60), 3)
    red = draw_screen("Approval Queue", (160, 60, 60), 3)
    empty = form_screen([])
    typed = form_screen(["Jane Smith", "SW1A 1AA", "07700 900123"])
    for name, img in (("green", green), ("red", red), ("empty", empty), ("typed", typed)):
        img.save(tmp_path / f"{name}.jpg", quality=85)
    empty.save(tmp_path / "empty_again.jpg", quality=60)  # same picture, heavier compression
    h = {p.stem: content_hash(p) for p in tmp_path.glob("*.jpg")}
    assert all(len(v) == 168 for v in h.values())
    assert hamming(h["green"], h["red"]) > 8
    assert hamming(h["empty"], h["typed"]) > 8
    assert hamming(h["empty"], h["empty_again"]) <= 2
    assert hamming(dhash(tmp_path / "green.jpg"), dhash(tmp_path / "red.jpg")) <= 6  # the old hash cannot tell them apart


def _kf(index: int, t: float) -> Keyframe:
    return Keyframe(index=index, timestamp=t, path=f"frames/frame_{index:04d}.jpg")


def test_build_moments_assigns_by_midpoint():
    keyframes = [_kf(0, 0.0), _kf(1, 10.0), _kf(2, 20.0)]
    segments = [
        TranscriptSegment(start=0.0, end=4.0, text="first"),
        TranscriptSegment(start=8.0, end=11.0, text="straddles, midpoint 9.5 -> first"),
        TranscriptSegment(start=9.0, end=13.0, text="straddles, midpoint 11 -> second"),
        TranscriptSegment(start=19.0, end=25.0, text="midpoint 22 -> third"),
        TranscriptSegment(start=40.0, end=41.0, text="after the end -> last"),
    ]
    moments = build_moments(keyframes, segments, duration=30.0)
    assert [m.keyframe_index for m in moments] == [0, 1, 2]
    assert [(m.start, m.end) for m in moments] == [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0)]
    assert [s.text for s in moments[0].segments] == ["first", "straddles, midpoint 9.5 -> first"]
    assert [s.text for s in moments[1].segments] == ["straddles, midpoint 11 -> second"]
    assert [s.text for s in moments[2].segments] == ["midpoint 22 -> third", "after the end -> last"]
    assert moments[2].text == "midpoint 22 -> third after the end -> last"


def test_build_moments_every_keyframe_gets_a_moment():
    keyframes = [_kf(0, 0.0), _kf(1, 5.0), _kf(2, 9.0), _kf(3, 14.0)]
    segments = [TranscriptSegment(start=6.0, end=7.0, text="only one line")]
    moments = build_moments(keyframes, segments, duration=20.0)
    assert len(moments) == 4
    assert [len(m.segments) for m in moments] == [0, 1, 0, 0]
    assert moments[-1].end == 20.0


def test_build_moments_with_nothing():
    assert build_moments([], [], duration=5.0) == []
    moments = build_moments([_kf(0, 0.0)], [], duration=5.0)
    assert len(moments) == 1 and moments[0].segments == []


def test_ingest_writes_and_reloads_recording(synthetic_video: Path, tmp_path: Path, capsys):
    vtt = tmp_path / "talk.vtt"
    vtt.write_text(VTT, encoding="utf-8")
    out = tmp_path / "out"

    recording = ingest(synthetic_video, out, transcript_path=vtt, max_width=400)
    assert isinstance(recording, Recording)
    assert recording.transcript_source == "file"
    assert 11.0 <= recording.duration <= 13.0
    assert 3 <= len(recording.keyframes) <= 6
    assert len(recording.segments) == 4
    assert len(recording.moments) == len(recording.keyframes)
    assert sum(len(m.segments) for m in recording.moments) == 4
    assert recording.moments[0].segments[0].text == "This is the customer search screen."

    written = out / "recording.json"
    assert written.exists()
    data = json.loads(written.read_text())
    assert data["transcript_source"] == "file"
    assert data["keyframes"][0]["path"] == "frames/frame_0000.jpg"
    assert (out / data["keyframes"][0]["path"]).exists()

    (out / "frames" / "frame_0000.jpg").unlink()  # prove the second call does no work
    capsys.readouterr()
    again = ingest(synthetic_video, out, transcript_path=vtt)
    assert again == recording
    assert "already exists" in capsys.readouterr().out
    assert not (out / "frames" / "frame_0000.jpg").exists()


def test_ingest_without_transcript(synthetic_video: Path, tmp_path: Path):
    recording = ingest(synthetic_video, tmp_path / "out", whisper_model=None, max_width=400)
    assert recording.transcript_source == "none"
    assert recording.segments == []
    assert all(m.segments == [] for m in recording.moments)
