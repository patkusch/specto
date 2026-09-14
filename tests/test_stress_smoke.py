"""Smoke test for scripts/stress_ingest.py: a three-minute synthetic recording through extract_keyframes.

The script itself is the tool for the hour-long run; this only checks that its
video builder works and that the hash detector finds the pages in it without
leaving sample files behind. Kept well under twenty seconds.
"""
from __future__ import annotations

import contextlib
import io
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.stress_ingest import build_video, leftover_files, make_screens, plan_visits  # noqa: E402
from specto.ingest import extract_keyframes, probe_duration  # noqa: E402

MINUTES = 3
SCREENS = 5


def test_plan_covers_every_screen_and_the_whole_length():
    rng = random.Random(4)
    screens = make_screens(SCREENS, rng)
    visits = plan_visits(MINUTES * 60, screens, rng)
    assert sorted(v.screen for v in visits) == list(range(SCREENS))
    assert visits[0].start == 0.0
    assert abs(visits[-1].end - MINUTES * 60) < 1e-6
    assert all(v.seconds > 0 for v in visits)
    assert len({s.title for s in screens}) == SCREENS


def test_three_minute_video_yields_one_frame_per_page(tmp_path: Path):
    # Typing and popups off: each is a real screen change and would add frames on top of the pages.
    video, visits, screens = build_video(
        tmp_path / "smoke.mp4", minutes=MINUTES, screens=SCREENS, seed=4, popup_chance=0.0, typing=False,
        log=lambda _: None,
    )
    assert video.exists() and video.stat().st_size > 0
    assert abs(probe_duration(video) - MINUTES * 60) < 1.0

    out_dir = tmp_path / "out"
    with contextlib.redirect_stdout(io.StringIO()):
        frames = extract_keyframes(video, out_dir)

    assert 4 <= len(frames) <= 8, [f.timestamp for f in frames]
    assert frames[0].timestamp == 0.0
    # Every page start should have a keyframe within two seconds of it (one sample per second, then the gap).
    for visit in visits[1:]:
        assert any(abs(f.timestamp - visit.start) <= 2.0 for f in frames), (visit.start, [f.timestamp for f in frames])

    frames_dir = out_dir / "frames"
    assert leftover_files(frames_dir) == []
    assert not list(frames_dir.glob("sample_*.jpg"))
    assert not list(frames_dir.glob("raw_*.jpg"))
    assert sorted(p.name for p in frames_dir.iterdir()) == [f"frame_{i:04d}.jpg" for i in range(len(frames))]
