"""Live mode, driven through replay so nothing touches the screen, the mic or the network."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from specto.fake import FakeCaller
from specto.live import LiveSession, analyze, replay, shot_time, take_screenshot
from specto.model import Analysis, Recording, TranscriptSegment, Word

# (time in seconds, screen): four distinct screens, each shown twice in a row.
SHOTS = [
    (0, "Customer Search"),
    (10, "Customer Search"),
    (20, "Customer Details"),
    (30, "Customer Details"),
    (40, "Approval Queue"),
    (50, "Approval Queue"),
    (60, "Done"),
    (70, "Done"),
]
SCREEN_STYLE = {
    "Customer Search": ((40, 70, 140), 1),
    "Customer Details": ((245, 245, 245), 5),
    "Approval Queue": ((30, 120, 60), 3),
    "Done": ((250, 235, 215), 0),
}
VTT = """WEBVTT

00:00:00.000 --> 00:00:05.000
This is the customer search screen.

00:00:12.000 --> 00:00:18.000
We type the postcode and press Find.

00:00:22.000 --> 00:00:28.000
Now the details page, the postcode is mandatory.

00:00:41.000 --> 00:00:47.000
Save sends it to the approvals queue.

00:01:02.000 --> 00:01:08.000
And that is the whole process.
"""


def make_shots(folder: Path, shots=SHOTS) -> Path:
    """Write the screenshots into `folder`, named by their time in seconds."""
    from conftest import draw_screen

    folder.mkdir(parents=True, exist_ok=True)
    for position, (seconds, title) in enumerate(shots):
        background, rows = SCREEN_STYLE[title]
        # Both accepted spellings on purpose: shot_0010.0.png and shot_20.png.
        name = f"shot_{seconds:06.1f}.png" if position % 2 else f"shot_{seconds}.png"
        draw_screen(title, background, rows).save(folder / name)
    return folder


@pytest.fixture
def replayed(tmp_path: Path):
    shots = make_shots(tmp_path / "shots")
    vtt = tmp_path / "call.vtt"
    vtt.write_text(VTT, encoding="utf-8")
    caller = FakeCaller()
    out = tmp_path / "out"
    session = replay(out, shots, vtt, caller, every_seconds=30, log=lambda _: None)
    return session, caller, out


def test_shot_time_accepts_both_spellings():
    assert shot_time("shot_0012.5.png") == 12.5
    assert shot_time("shot_12.png") == 12
    assert shot_time(Path("x/shot_007.jpg")) == 7
    with pytest.raises(ValueError):
        shot_time("screenshot.png")


def test_replay_keeps_only_changed_screens_and_writes_everything(replayed):
    session, caller, out = replayed
    recording = Recording.model_validate_json((out / "recording.json").read_text())
    assert [k.timestamp for k in recording.keyframes] == [0, 20, 40, 60]
    assert [k.path for k in recording.keyframes] == [f"frames/frame_{i:04d}.jpg" for i in range(4)]
    assert all((out / k.path).exists() for k in recording.keyframes)
    assert recording.source == "live"
    assert recording.duration == 70
    assert recording.transcript_source == "file"
    assert len(recording.segments) == 5
    assert sum(len(m.segments) for m in recording.moments) == 5
    assert recording.keyframes[0].change_from_previous is None
    assert all(k.change_from_previous is not None for k in recording.keyframes[1:])
    for name in ("analysis.json", "analysis.xlsx", "report.md", "live_questions.md"):
        assert (out / name).exists(), name
    assert not (out / "frames" / "pending.jpg").exists()


def test_replay_analyses_at_each_boundary_and_at_the_end(replayed):
    session, caller, out = replayed
    # Boundaries at 30 and 60 seconds of recording time, then once more at the end.
    assert session.analysis_rounds == 3
    # Each round is one chunk call plus one consolidation call with the fake model.
    assert len(caller.calls) == 6
    assert isinstance(session.last_analysis, Analysis)
    saved = Analysis.model_validate_json((out / "analysis.json").read_text())
    assert saved.questions == session.last_analysis.questions


def test_live_questions_lists_newest_first(replayed):
    session, caller, out = replayed
    text = (out / "live_questions.md").read_text()
    assert text.startswith("# Questions to ask before the call ends")
    ids = re.findall(r"^## (Q\d+) at (\d\d:\d\d)", text, re.M)
    assert [i for i, _ in ids] == ["Q002", "Q001"]
    times = [t for _, t in ids]
    assert times == sorted(times, reverse=True)
    by_id = {q.id: q for q in session.last_analysis.questions}
    for qid, _ in ids:
        assert by_id[qid].question in text
        assert by_id[qid].why_it_matters in text


def test_load_resumes_and_continues_numbering(replayed):
    from conftest import draw_screen

    session, caller, out = replayed
    resumed = LiveSession.load(out)
    assert len(resumed.recording.keyframes) == 4
    assert resumed.recording.duration == 70
    # The same screen as the last kept frame is dropped, a new one carries on the numbering.
    assert resumed.add_screenshot(draw_screen("Done", (250, 235, 215), 0), 75) is None
    kept = resumed.add_screenshot(draw_screen("Reports", (90, 90, 120), 2), 80)
    assert kept is not None
    assert kept.index == 4
    assert kept.path == "frames/frame_0004.jpg"
    assert (out / kept.path).exists()
    resumed.save()
    again = Recording.model_validate_json((out / "recording.json").read_text())
    assert len(again.keyframes) == 5
    assert again.duration == 80
    assert len(again.moments) == 5


def test_add_screenshot_keeps_first_and_respects_min_gap(tmp_path: Path):
    from conftest import draw_screen

    session = LiveSession(tmp_path / "out", min_gap=5)
    a = draw_screen("Customer Search", (40, 70, 140), 1)
    b = draw_screen("Done", (250, 235, 215), 0)
    assert session.add_screenshot(a, 0).index == 0
    assert session.add_screenshot(a, 1) is None  # same screen
    assert session.add_screenshot(b, 2) is None  # changed, but inside the gap
    assert session.add_screenshot(b, 6).index == 1
    assert session.screenshots_seen == 4


def test_add_screenshot_scales_wide_images(tmp_path: Path):
    from PIL import Image

    session = LiveSession(tmp_path / "out")
    kept = session.add_screenshot(Image.new("RGB", (2560, 1440), (10, 20, 30)), 0)
    assert (kept.width, kept.height) == (1280, 720)
    with Image.open(tmp_path / "out" / kept.path) as img:
        assert img.size == (1280, 720)


def test_add_audio_chunk_shifts_times(tmp_path: Path, monkeypatch):
    seen = {}

    def fake_transcribe(path, model_size="base"):
        seen["path"] = Path(path)
        seen["model_size"] = model_size
        return [
            TranscriptSegment(
                start=0.5, end=2.0, text="press save",
                words=[Word(start=0.5, end=1.0, text="press"), Word(start=1.2, end=2.0, text="save")],
            ),
            TranscriptSegment(start=3.0, end=4.0, text="done"),
        ]

    monkeypatch.setattr("specto.transcript.transcribe", fake_transcribe)
    session = LiveSession(tmp_path / "out")
    added = session.add_audio_chunk(tmp_path / "chunk_0001.wav", start_time=30, model_size="small")
    assert seen == {"path": tmp_path / "chunk_0001.wav", "model_size": "small"}
    assert [(s.start, s.end) for s in added] == [(30.5, 32.0), (33.0, 34.0)]
    assert [(w.start, w.end, w.text) for w in added[0].words] == [(30.5, 31.0, "press"), (31.2, 32.0, "save")]
    assert session.recording.segments == added
    assert session.recording.transcript_source == "whisper"
    session.save()
    assert session.recording.duration == 34.0


def test_analyze_runs_only_when_due(tmp_path: Path):
    from conftest import draw_screen

    caller = FakeCaller()
    session = LiveSession(tmp_path / "out")
    quiet = lambda _: None
    assert analyze(session, caller, every_seconds=60, log=quiet) is None  # nothing to look at yet
    session.add_screenshot(draw_screen("Customer Search", (40, 70, 140), 1), 0)
    assert analyze(session, caller, every_seconds=60, log=quiet) is not None  # first ever run
    assert analyze(session, caller, every_seconds=60, log=quiet) is None  # nothing new, not due
    session.add_screenshot(draw_screen("Done", (250, 235, 215), 0), 30)
    assert analyze(session, caller, every_seconds=60, log=quiet) is None  # new frame but only 30s passed
    assert analyze(session, caller, every_seconds=60, force=True, log=quiet) is not None  # forced, new frame
    assert analyze(session, caller, every_seconds=60, force=True, log=quiet) is None  # forced, nothing new
    session.add_segments([TranscriptSegment(start=80, end=95, text="and that is it")])
    assert analyze(session, caller, every_seconds=60, log=quiet) is not None  # 65s passed
    assert session.analysis_rounds == 3


def test_take_screenshot_reports_missing_command(tmp_path: Path):
    ok, error = take_screenshot(tmp_path / "shot.jpg", command=str(tmp_path / "no-such-screencapture"))
    assert ok is False
    assert "not found" in error
    assert not (tmp_path / "shot.jpg").exists()
