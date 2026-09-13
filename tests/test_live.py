"""Live mode, driven through replay so nothing touches the screen, the mic or the network."""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

from specto import live
from specto.fake import FakeCaller
from specto.live import (
    LiveSession,
    analyze,
    audio_input_args,
    parse_dshow_devices,
    replay,
    screenshot_backends,
    shot_time,
    take_screenshot,
)
from specto.model import Analysis, Question, Recording, TranscriptSegment, Word

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


def test_live_questions_lists_blocking_first_then_newest(replayed):
    session, caller, out = replayed
    text = (out / "live_questions.md").read_text()
    assert text.startswith("# Questions to ask before the call ends")
    ids = re.findall(r"^## (Q\d+) at (\d\d:\d\d)", text, re.M)
    # Q001 is older but blocks R001, so it comes before the newer Q002, which blocks nothing.
    assert [i for i, _ in ids] == ["Q001", "Q002"]
    by_id = {q.id: q for q in session.last_analysis.questions}
    (blocked,) = by_id["Q001"].blocks_requirement_ids
    assert by_id["Q002"].blocks_requirement_ids == []
    assert by_id["Q001"].timestamp < by_id["Q002"].timestamp
    for qid, _ in ids:
        assert by_id[qid].question in text
        assert by_id[qid].why_it_matters in text
    assert f"(blocks {blocked})" in text
    q2 = text[text.index("## Q002"):]
    assert "(blocks" not in q2


def test_live_questions_block_ids_are_renumbered(replayed):
    session, caller, out = replayed
    by_id = {q.id: q for q in session.last_analysis.questions}
    requirement_ids = {r.id for r in session.last_analysis.requirements}
    assert by_id["Q001"].blocks_requirement_ids == ["R001"]
    assert set(by_id["Q001"].blocks_requirement_ids) <= requirement_ids


def test_write_live_questions_orders_by_blocks_then_newest(tmp_path: Path):
    from specto.live import write_live_questions

    base = dict(why_it_matters="Because.", keyframe_index=0, screen_id=None, category="other")
    analysis = Analysis(
        title="t", summary="s",
        questions=[
            Question(id="Q001", question="Old, blocks none", timestamp=10, blocks_requirement_ids=[], **base),
            Question(id="Q002", question="Newer, blocks none", timestamp=50, blocks_requirement_ids=[], **base),
            Question(id="Q003", question="Old, blocks two", timestamp=20, blocks_requirement_ids=["R001", "R003"], **base),
            Question(id="Q004", question="Newest, blocks one", timestamp=60, blocks_requirement_ids=["R002"], **base),
        ],
    )
    text = write_live_questions(analysis, tmp_path / "q.md", 70).read_text()
    ids = re.findall(r"^## (Q\d+) at", text, re.M)
    assert ids == ["Q003", "Q004", "Q002", "Q001"]
    assert "Old, blocks two (blocks R001, R003)" in text
    assert "Newest, blocks one (blocks R002)" in text
    assert "Newer, blocks none\n" in text


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


# ------------------------------------------------------------ screen and mic


def _fake_mss_module(width: int = 64, height: int = 48, fail: Exception | None = None) -> types.ModuleType:
    """A stand-in for the mss package: one monitor, a solid red frame, or a grab that raises."""

    class Shot:
        size = (width, height)
        bgra = bytes([0, 0, 255, 255]) * (width * height)  # B, G, R, X: red

    class MSS:
        monitors = [{"left": 0, "top": 0, "width": width, "height": height}] * 2

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def grab(self, monitor):
            if fail:
                raise fail
            assert monitor is self.monitors[1]
            return Shot()

    module = types.ModuleType("mss")
    module.MSS = MSS
    return module


def test_take_screenshot_uses_mss_when_installed(tmp_path: Path, monkeypatch):
    from PIL import Image

    monkeypatch.setitem(sys.modules, "mss", _fake_mss_module())
    monkeypatch.setattr(live, "macos_screen_recording_allowed", lambda: True)
    shot = tmp_path / "shot.jpg"
    ok, error = take_screenshot(shot, platform="darwin", command=str(tmp_path / "no-such-screencapture"))
    assert ok is True and error == ""
    with Image.open(shot) as img:
        assert img.format == "JPEG" and img.size == (64, 48)
        assert img.convert("RGB").getpixel((10, 10))[0] > 200  # red came through the BGRX unpacking
    ok, error = take_screenshot(shot, display=3, backend="mss", platform="win32")
    assert ok is False and error.startswith("mss: no display 3; this machine has 1")


def test_take_screenshot_mss_refuses_without_mac_permission(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", _fake_mss_module())
    monkeypatch.setattr(live, "macos_screen_recording_allowed", lambda: False)
    ok, error = take_screenshot(tmp_path / "shot.jpg", backend="mss", platform="darwin")
    assert ok is False
    assert error.startswith("mss: Screen Recording permission not granted") and "wallpaper" in error
    assert "System Settings > Privacy & Security > Screen Recording" in error
    assert not (tmp_path / "shot.jpg").exists()


def test_take_screenshot_falls_back_to_screencapture_and_names_it(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", None)  # `import mss` raises ImportError
    fake = tmp_path / "screencapture"
    fake.write_text("#!/bin/sh\necho 'could not create image from display' >&2\nexit 1\n")
    fake.chmod(0o755)
    ok, error = take_screenshot(tmp_path / "shot.jpg", command=str(fake), platform="darwin")
    assert ok is False
    assert 'mss: not installed (pip install "specto[live]")' in error
    assert "screencapture: could not create image from display" in error
    assert "Screen Recording permission" in error
    assert not (tmp_path / "shot.jpg").exists()


def test_take_screenshot_reports_missing_command(tmp_path: Path):
    ok, error = take_screenshot(
        tmp_path / "shot.jpg", command=str(tmp_path / "no-such-screencapture"), backend="screencapture"
    )
    assert ok is False
    assert error.startswith("screencapture: ") and "not found" in error
    assert not (tmp_path / "shot.jpg").exists()


def test_take_screenshot_grim_missing_on_linux_says_how_to_install(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(live, "GRIM", str(tmp_path / "no-such-grim"))
    ok, error = take_screenshot(tmp_path / "shot.jpg", backend="grim", platform="linux")
    assert ok is False
    assert error.startswith("grim: ") and "not found" in error
    assert "apt install grim" in error and "Wayland" in error
    assert "On Linux install" in error
    with pytest.raises(ValueError):
        take_screenshot(tmp_path / "shot.jpg", backend="xdotool")


def test_take_screenshot_rejects_an_empty_file(tmp_path: Path):
    fake = tmp_path / "screencapture"
    fake.write_text("#!/bin/sh\n: > \"$5\"\nexit 0\n")  # exits 0 but writes an empty file
    fake.chmod(0o755)
    ok, error = take_screenshot(tmp_path / "shot.jpg", command=str(fake), backend="screencapture")
    assert ok is False and "screencapture: no image written" in error
    assert not (tmp_path / "shot.jpg").exists()


def test_screenshot_backend_order_per_platform():
    assert screenshot_backends("darwin") == ["mss", "screencapture"]
    assert screenshot_backends("Darwin") == ["mss", "screencapture"]
    assert screenshot_backends("win32") == ["mss", "powershell"]
    assert screenshot_backends("Windows") == ["mss", "powershell"]
    assert screenshot_backends("linux", wayland=False) == ["mss", "grim", "import"]
    assert screenshot_backends("Linux", wayland=True) == ["grim", "mss", "import"]


def test_powershell_script_uses_system_drawing(tmp_path: Path):
    script = live.powershell_screenshot_script(tmp_path / "it's.jpg", 1)
    assert "[System.Windows.Forms.Screen]::PrimaryScreen" in script and "System.Drawing" in script
    assert "it''s.jpg" in script and "\n" not in script
    assert "AllScreens[1]" in live.powershell_screenshot_script(tmp_path / "x.jpg", 2)


def test_audio_input_args_per_platform():
    assert audio_input_args("darwin") == ["-f", "avfoundation", "-i", ":0"]
    assert audio_input_args("darwin", ":1") == ["-f", "avfoundation", "-i", ":1"]
    assert audio_input_args("win32", "Microphone (Realtek Audio)") == [
        "-f", "dshow", "-i", "audio=Microphone (Realtek Audio)"
    ]
    with pytest.raises(ValueError, match="dshow"):
        audio_input_args("win32")  # the default marker is not a dshow name
    assert audio_input_args("linux") == ["-f", "pulse", "-i", "default"]
    assert audio_input_args("linux", "alsa_input.pci-0000_00_1f.3.analog-stereo") == [
        "-f", "pulse", "-i", "alsa_input.pci-0000_00_1f.3.analog-stereo"
    ]
    assert audio_input_args("linux", "hw:0") == ["-f", "alsa", "-i", "hw:0"]
    assert audio_input_args("Linux", "plughw:1,0") == ["-f", "alsa", "-i", "plughw:1,0"]


def test_parse_dshow_devices_both_ffmpeg_spellings():
    new_style = (
        "[dshow @ 000001] \"Integrated Camera\" (video)\n"
        "[dshow @ 000001]   Alternative name \"@device_pnp_x\"\n"
        "[dshow @ 000001] \"Microphone (Realtek Audio)\" (audio)\n"
        "[dshow @ 000001]   Alternative name \"@device_cm_y\"\n"
        "[dshow @ 000001] \"Headset (Jabra)\" (audio)\n"
        "dummy: Immediate exit requested\n"
    )
    assert parse_dshow_devices(new_style) == ["Microphone (Realtek Audio)", "Headset (Jabra)"]
    old_style = (
        "[dshow @ 000001] DirectShow video devices (some may be both video and audio devices)\n"
        "[dshow @ 000001]  \"Integrated Camera\"\n"
        "[dshow @ 000001] DirectShow audio devices\n"
        "[dshow @ 000001]  \"Microphone (Realtek Audio)\"\n"
        "[dshow @ 000001]     Alternative name \"@device_cm_y\"\n"
    )
    assert parse_dshow_devices(old_style) == ["Microphone (Realtek Audio)"]
    assert parse_dshow_devices("") == []


def test_resolve_audio_device_picks_first_dshow_mic_and_says_so(monkeypatch):
    monkeypatch.setattr(live, "list_dshow_audio_devices", lambda: ["Headset (Jabra)", "Line In"])
    lines: list[str] = []
    assert live.resolve_audio_device("win32", ":0", log=lines.append) == "Headset (Jabra)"
    assert lines and "Headset (Jabra)" in lines[0] and "first microphone" in lines[0]
    assert live.resolve_audio_device("win32", "Line In", log=lines.append) == "Line In"
    assert live.resolve_audio_device("darwin", ":0", log=lines.append) == ":0"
    assert len(lines) == 1  # only the automatic pick is logged
    monkeypatch.setattr(live, "list_dshow_audio_devices", lambda: [])
    with pytest.raises(RuntimeError, match="no dshow audio device"):
        live.resolve_audio_device("win32", ":0", log=lines.append)


def test_start_audio_recorder_builds_the_platform_command(tmp_path: Path, monkeypatch):
    calls: list[list[str]] = []

    class FakeProc:
        def __init__(self, argv, **kwargs):
            calls.append(argv)

    monkeypatch.setattr(live.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(live, "ffmpeg_exe", lambda: "/x/ffmpeg")
    monkeypatch.setattr(live, "list_dshow_audio_devices", lambda: ["Mic A"])
    live.start_audio_recorder(tmp_path / "a.wav", 30, platform="darwin", log=lambda _: None)
    live.start_audio_recorder(tmp_path / "b.wav", 30, platform="win32", log=lambda _: None)
    live.start_audio_recorder(tmp_path / "c.wav", 2.5, "hw:1", platform="linux", log=lambda _: None)
    assert calls[0][:1] == ["/x/ffmpeg"] and calls[0][-8:-2] == [":0", "-t", "30", "-ac", "1", "-ar"]
    assert "avfoundation" in calls[0]
    assert calls[1][calls[1].index("-f") + 1] == "dshow" and "audio=Mic A" in calls[1]
    assert calls[2][calls[2].index("-f") + 1] == "alsa" and "hw:1" in calls[2] and "2.5" in calls[2]
