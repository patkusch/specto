"""Stage 1 tests: duration, keyframes, alignment and the ingest wrapper.

Everything runs offline against the synthetic videos from conftest.py.
"""
from __future__ import annotations

import json
from pathlib import Path

from specto.align import build_moments
import pytest

from specto.ingest import (
    content_hash,
    detect_share_region,
    dhash,
    extract_keyframes,
    hamming,
    ingest,
    ingest_folder,
    probe_duration,
    screenshot_times,
    split_notes,
    spread_notes,
)
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
    # The same holds when max_frames thins the kept ones further (frame 0 stays, the
    # survivor is whichever screen differs most from its neighbours, not the last by time).
    frames = extract_keyframes(synthetic_video, tmp_path, detect="hash", max_frames=2)
    assert len(frames) == 2 and frames[0].timestamp == 0.0 and frames[1].timestamp in (3.0, 6.0, 9.0)
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


# ---------------------------------------------------------- cropping to the shared window

def _close(box, expected, tolerance=4):
    return all(abs(a - b) <= tolerance for a, b in zip(box, expected))


def test_detect_share_region_finds_the_shared_window(bordered_video: Path):
    """Border, toolbar and the 6x6 blinking dot are outside the box; the shared screens are inside."""
    from conftest import SHARE_BOX

    box = detect_share_region(bordered_video)
    assert box is not None and _close(box, SHARE_BOX), box
    assert all(v % 2 == 0 for v in box)


def test_detect_share_region_leaves_a_full_frame_alone(synthetic_video: Path, static_video: Path, capsys):
    assert detect_share_region(synthetic_video) is None  # the whole picture changes: nothing to crop
    assert "no border to crop" in capsys.readouterr().out
    assert detect_share_region(static_video) is None  # nothing changes at all
    assert "nothing in the picture changes" in capsys.readouterr().out


def test_detect_share_region_leaves_thin_margins_of_a_full_frame_app_alone(phone_video: Path, capsys):
    """A portrait app with static margins of about 4% is not a meeting frame: nothing is trimmed."""
    assert detect_share_region(phone_video) is None
    out = capsys.readouterr().out
    assert "under 5%" in out and "no border to crop" in out


def test_detect_share_region_trims_only_the_sides_with_a_wide_margin(gallery_video: Path, capsys):
    from conftest import GALLERY_BOX

    box = detect_share_region(gallery_video)
    assert box is not None
    x, y, w, h = box
    assert _close((x, w), (GALLERY_BOX[0], GALLERY_BOX[2]))  # left and right trimmed
    assert (y, h) == (0, 540)  # top and bottom kept whole
    out = capsys.readouterr().out
    assert "trimming left 160 px, right 160 px" in out and "kept top, bottom" in out


def test_detect_share_region_states_every_side_it_trims(bordered_video: Path, capsys):
    detect_share_region(bordered_video)
    out = capsys.readouterr().out
    assert "trimming left 160 px, top 60 px, right 160 px, bottom 120 px" in out
    assert "kept" not in out


def test_detect_share_region_on_the_two_example_recordings(capsys):
    """The Teams-style claims recording is cropped exactly; the phone app keeps its left, top and right edges."""
    root = Path(__file__).resolve().parent.parent / "examples"
    assert detect_share_region(root / "claims" / "walkthrough.mp4") == (160, 60, 1280, 720)
    x, y, w, _ = detect_share_region(root / "deliveries" / "walkthrough.mp4")
    assert (x, y, w) == (0, 0, 540)  # status bar and side edges intact; only the blank strip under the content goes
    assert "kept left, top, right" in capsys.readouterr().out


def test_crop_auto_keeps_the_shared_window_only(bordered_video: Path, tmp_path: Path, capsys):
    from conftest import SHARE_BOX

    frames = extract_keyframes(bordered_video, tmp_path / "auto", detect="hash", crop="auto")
    out = capsys.readouterr().out
    assert "cropping to the 640x360 area at 160,60, the rest of the frame never changes" in out
    assert [f.timestamp for f in frames] == [0.0, 3.0, 6.0, 9.0]
    assert all(_close((f.width, f.height), SHARE_BOX[2:]) for f in frames), [(f.width, f.height) for f in frames]
    # An explicit box does the same, and the scene detector gets the same crop.
    explicit = extract_keyframes(bordered_video, tmp_path / "box", detect="scene", crop=SHARE_BOX)
    assert "cropping to the 640x360 area at 160,60" in capsys.readouterr().out
    assert [f.timestamp for f in explicit] == [0.0, 3.0, 6.0, 9.0]
    assert [(f.width, f.height) for f in explicit] == [SHARE_BOX[2:]] * 4
    # Without a crop the frames are the whole 960x540 meeting picture.
    whole = extract_keyframes(bordered_video, tmp_path / "whole", detect="hash")
    assert [(f.width, f.height) for f in whole] == [(960, 540)] * len(whole)


def test_crop_applies_to_the_static_fallback_and_scales_after(static_video: Path, tmp_path: Path):
    frames = extract_keyframes(static_video, tmp_path, crop=(100, 50, 400, 200), max_width=200, fallback_interval=5)
    assert len(frames) >= 4
    assert [(f.width, f.height) for f in frames] == [(200, 100)] * len(frames)


def test_bad_crop_is_refused(synthetic_video: Path, tmp_path: Path):
    with pytest.raises(ValueError, match="crop must be"):
        extract_keyframes(synthetic_video, tmp_path, crop="middle")
    with pytest.raises(ValueError, match="crop must be"):
        extract_keyframes(synthetic_video, tmp_path, crop=(1, 2, 3))
    with pytest.raises(ValueError, match="w, h > 0"):
        extract_keyframes(synthetic_video, tmp_path, crop=(0, 0, 0, 100))


# ---------------------------------------------------------- long recordings: max_frames

def test_max_frames_drops_the_least_changed_frames_first(variants_video: Path, tmp_path: Path, capsys):
    """12 screens, each followed by a near-identical variant: the cap keeps the 12 screens, not every other frame."""
    frames = extract_keyframes(variants_video, tmp_path / "all", detect="hash")
    assert len(frames) == 24  # the variants differ enough to be kept by the detector
    capsys.readouterr()

    frames = extract_keyframes(variants_video, tmp_path / "capped", detect="hash", max_frames=12)
    out = capsys.readouterr().out
    assert [f.timestamp for f in frames] == [float(t) for t in range(0, 48, 4)]
    line = next(l for l in out.splitlines() if "dropped 12 frames that differed from their neighbour by" in l)
    largest = int(line.split(" by ")[1].split(" ")[0])
    assert 8 < largest < 40, line  # the variants, not a screen change (those are 70 bits and more apart)
    assert sorted(p.name for p in (tmp_path / "capped" / "frames").iterdir()) == [f"frame_{i:04d}.jpg" for i in range(12)]


def test_content_hash_sees_colour_and_text_but_not_noise(tmp_path: Path):
    from conftest import draw_screen, form_screen

    green = draw_screen("Approval Queue", (60, 160, 60), 3)
    red = draw_screen("Approval Queue", (160, 60, 60), 3)
    empty = form_screen([])
    typed = form_screen(["Jane Smith", "SW1A 1AA", "07000 12345678"])
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
    moments = build_moments(keyframes, segments, duration=30.0, estimate_word_times=False)
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


# ---------------------------------------------------------- screenshot folders

# Six distinct screens for the folder tests: colour and row count both differ.
FOLDER_SCREENS = [
    ("Customer Search", (40, 70, 140), 1),
    ("Customer Details", (245, 245, 245), 5),
    ("Approval Queue", (30, 120, 60), 3),
    ("Done", (250, 235, 215), 0),
    ("Reports", (90, 90, 120), 2),
    ("Settings", (160, 60, 60), 4),
]
NOTES_MD = """# Onboarding walkthrough

First we open the customer search and type the postcode.

Then the details page opens with every field.

Save sends the record to the approval queue.

The done page confirms it,
and shows the reference number.

Finally the reports page lists what was approved today.
"""


def _write_shots(folder: Path, names: list[str], screens=None) -> Path:
    """Write one screenshot per name, drawing FOLDER_SCREENS (or `screens`) in order."""
    from conftest import draw_screen

    folder.mkdir(parents=True, exist_ok=True)
    for name, (title, background, rows) in zip(names, screens or FOLDER_SCREENS):
        draw_screen(title, background, rows).save(folder / name)
    return folder


def test_screenshot_times_reads_seconds_and_spaces_the_rest(tmp_path: Path):
    log: list[str] = []
    names = ["shot_0.png", "shot_10.png", "shot_0025.0.png", "IMG_0001.png", "IMG_0002.png", "IMG_0003.png"]
    paths = [tmp_path / n for n in names]
    timed = screenshot_times(paths, log=log.append)
    assert [(p.name, t) for p, t in timed] == [
        ("shot_0.png", 0.0), ("shot_10.png", 10.0), ("shot_0025.0.png", 25.0),
        ("IMG_0001.png", 35.0), ("IMG_0002.png", 45.0), ("IMG_0003.png", 55.0),
    ]
    assert log == ["ingest: 3 screenshot names carry a time; the other 3 follow in name order at 10s steps after 25s"]


def test_screenshot_times_with_no_times_uses_natural_name_order(tmp_path: Path):
    log: list[str] = []
    paths = [tmp_path / n for n in ("IMG_10.png", "IMG_2.png", "IMG_1.png", "Screenshot (3).png")]
    timed = screenshot_times(paths, log=log.append)
    assert [(p.name, t) for p, t in timed] == [
        ("IMG_1.png", 0.0), ("IMG_2.png", 10.0), ("IMG_10.png", 20.0), ("Screenshot (3).png", 30.0),
    ]
    assert log == ["ingest: no screenshot name carries a time, so they are spaced 10s apart in name order"]


def test_screenshot_times_reads_iso_timestamps_relative_to_the_earliest(tmp_path: Path):
    names = [
        "2026-09-12T10-05-03.png",
        "2026-09-12T10-04-33.png",
        "Screenshot 2026-09-12 at 10.04.45 AM.png",  # macOS spelling
        "Screenshot 2026-09-12 100530.png",  # Windows spelling
    ]
    timed = screenshot_times([tmp_path / n for n in names], log=lambda _: None)
    assert [(p.name, t) for p, t in timed] == [
        ("2026-09-12T10-04-33.png", 0.0),
        ("Screenshot 2026-09-12 at 10.04.45 AM.png", 12.0),
        ("2026-09-12T10-05-03.png", 30.0),
        ("Screenshot 2026-09-12 100530.png", 57.0),
    ]


def test_ingest_folder_mixed_names_drops_the_repeat(tmp_path: Path):
    """Three timed names, three IMG names, and the fifth screenshot repeats the fourth."""
    screens = FOLDER_SCREENS[:4] + [FOLDER_SCREENS[3]] + [FOLDER_SCREENS[4]]
    names = ["shot_0.png", "shot_10.png", "shot_0025.0.png", "IMG_0001.png", "IMG_0002.png", "IMG_0003.png"]
    shots = _write_shots(tmp_path / "shots", names, screens)
    log: list[str] = []

    recording = ingest_folder(shots, tmp_path / "out", log=log.append)
    assert isinstance(recording, Recording)
    assert recording.source == "shots"
    assert [k.timestamp for k in recording.keyframes] == [0.0, 10.0, 25.0, 35.0, 55.0]
    assert [k.path for k in recording.keyframes] == [f"frames/frame_{i:04d}.jpg" for i in range(5)]
    assert [k.index for k in recording.keyframes] == [0, 1, 2, 3, 4]
    assert all((tmp_path / "out" / k.path).exists() for k in recording.keyframes)
    assert recording.duration == 65.0
    assert recording.transcript_source == "none"
    assert recording.segments == []
    assert [m.keyframe_index for m in recording.moments] == [0, 1, 2, 3, 4]
    assert [(m.start, m.end) for m in recording.moments] == [(0, 10), (10, 25), (25, 35), (35, 55), (55, 65)]
    assert recording.keyframes[0].change_from_previous is None
    assert all(k.change_from_previous is not None for k in recording.keyframes[1:])
    assert any("the other 3 follow in name order at 10s steps after 25s" in line for line in log)
    assert any("6 screenshots" in line and "5 kept" in line and "1 dropped" in line for line in log)
    assert any("no transcript given" in line for line in log)
    assert sorted(p.name for p in (tmp_path / "out" / "frames").iterdir() if p.name.startswith("frame_")) == [
        f"frame_{i:04d}.jpg" for i in range(5)
    ]
    assert not (tmp_path / "out" / "frames" / "pending.jpg").exists()
    written = json.loads((tmp_path / "out" / "recording.json").read_text())
    assert written["source"] == "shots" and len(written["keyframes"]) == 5


def test_ingest_folder_iso_names(tmp_path: Path):
    names = ["2026-09-12T10-04-33.png", "2026-09-12T10-04-45.png", "2026-09-12T10-05-03.png"]
    shots = _write_shots(tmp_path / "walk", names)
    recording = ingest_folder(shots, tmp_path / "out", log=lambda _: None)
    assert [k.timestamp for k in recording.keyframes] == [0.0, 12.0, 30.0]
    assert recording.duration == 40.0
    assert recording.source == "walk"


def test_split_notes_paragraphs_bullets_and_headings():
    notes = split_notes(NOTES_MD)
    assert notes == [
        "Onboarding walkthrough: First we open the customer search and type the postcode.",
        "Then the details page opens with every field.",
        "Save sends the record to the approval queue.",
        "The done page confirms it, and shows the reference number.",
        "Finally the reports page lists what was approved today.",
    ]
    bulleted = "Steps\n\n- open search\n- type postcode\n\n1. press Find\n2) read the list\n\n## Later\n\nplain paragraph\n"
    assert split_notes(bulleted) == ["Steps", "open search", "type postcode", "press Find", "read the list", "Later: plain paragraph"]
    assert split_notes("\n\n  \n") == []


def test_spread_notes_matches_counts_or_goes_proportional():
    frames = [_kf(0, 0.0), _kf(1, 10.0), _kf(2, 25.0)]
    same = spread_notes(["a", "b", "c"], frames, duration=35.0)
    assert [(s.start, s.end, s.text) for s in same] == [(0.0, 10.0, "a"), (10.0, 25.0, "b"), (25.0, 35.0, "c")]
    fewer = spread_notes(["a", "b"], frames, duration=35.0)
    assert [(s.start, s.end, s.text) for s in fewer] == [(0.0, 10.0, "a"), (10.0, 25.0, "b")]
    more = spread_notes(["a", "b", "c", "d", "e"], frames, duration=35.0)
    assert [(s.start, s.end, s.text) for s in more] == [
        (0.0, 5.0, "a"), (5.0, 10.0, "b"), (10.0, 17.5, "c"), (17.5, 25.0, "d"), (25.0, 35.0, "e"),
    ]  # 2, 2, 1: the frames fill at the same pace as the notes
    assert spread_notes([], frames, 35.0) == [] and spread_notes(["a"], [], 35.0) == []


def test_ingest_folder_spreads_untimed_notes_over_the_frames(tmp_path: Path):
    shots = _write_shots(tmp_path / "shots", [f"shot_{t}.png" for t in (0, 10, 25, 40, 70)])
    notes = tmp_path / "notes.md"
    notes.write_text(NOTES_MD, encoding="utf-8")
    log: list[str] = []

    recording = ingest_folder(shots, tmp_path / "out", transcript_path=notes, log=log.append)
    assert recording.transcript_source == "file"
    assert len(recording.keyframes) == 5
    assert len(recording.segments) == 5
    assert [(s.start, s.end) for s in recording.segments] == [(0, 10), (10, 25), (25, 40), (40, 70), (70, 80)]
    assert recording.segments[0].text.startswith("Onboarding walkthrough: First we open the customer search")
    assert recording.segments[-1].text == "Finally the reports page lists what was approved today."
    assert [len(m.segments) for m in recording.moments] == [1, 1, 1, 1, 1]
    for moment, segment in zip(recording.moments, recording.segments):
        assert moment.segments == [segment]
        assert (moment.start, moment.end) == (segment.start, segment.end)
    assert any("notes.md has no timestamps, so its 5 notes are spread over the 5 frames in order" in line for line in log)


def test_ingest_folder_still_parses_a_timed_transcript(tmp_path: Path):
    shots = _write_shots(tmp_path / "shots", [f"shot_{t}.png" for t in (0, 3, 6, 9)])
    vtt = tmp_path / "talk.vtt"
    vtt.write_text(VTT, encoding="utf-8")
    log: list[str] = []

    recording = ingest_folder(shots, tmp_path / "out", transcript_path=vtt, log=log.append)
    assert recording.transcript_source == "file"
    assert [s.text for s in recording.segments] == [
        "This is the customer search screen.",
        "Now the customer details with all the fields.",
        "Then it lands in the approval queue.",
        "And we are done.",
    ]
    assert [len(m.segments) for m in recording.moments] == [1, 1, 1, 1]
    assert recording.moments[1].segments[0].start == 3.2  # placed by its own time, not spread
    assert any("talk.vtt carries its own times" in line for line in log)
    assert not any("spread over" in line for line in log)


def test_ingest_folder_reuses_unless_forced(tmp_path: Path):
    shots = _write_shots(tmp_path / "shots", [f"shot_{t}.png" for t in (0, 10, 20)])
    out = tmp_path / "out"
    first = ingest_folder(shots, out, log=lambda _: None)
    assert len(first.keyframes) == 3

    (out / "frames" / "frame_0000.jpg").unlink()  # prove the second call does no work
    log: list[str] = []
    again = ingest_folder(shots, out, log=log.append)
    assert again == first
    assert any("already exists" in line for line in log)
    assert not (out / "frames" / "frame_0000.jpg").exists()

    forced = ingest_folder(shots, out, force=True, log=lambda _: None)
    assert forced == first
    assert (out / "frames" / "frame_0000.jpg").exists()
    assert sorted(p.name for p in (out / "frames").iterdir() if p.name.startswith("frame_")) == [
        f"frame_{i:04d}.jpg" for i in range(3)
    ]


def test_ingest_folder_with_no_screenshots_fails_loudly(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "notes.txt").write_text("nothing to see", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="no screenshots"):
        ingest_folder(empty, tmp_path / "out", log=lambda _: None)


def test_rerun_with_fewer_frames_removes_stale_close_ups(synthetic_video, tmp_path):
    from specto.ingest import extract_keyframes

    extract_keyframes(synthetic_video, tmp_path)
    stale = tmp_path / "frames" / "crop_0099.jpg"
    stale.write_bytes(b"old close-up")
    extract_keyframes(synthetic_video, tmp_path)
    assert not stale.exists()
