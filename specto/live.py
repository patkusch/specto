"""Live mode: build the recording while the call is still going.

During a call, specto grabs the shared screen every few seconds and records
the microphone in short chunks. Both go into a `LiveSession`, a store shaped
exactly like the `recording.json` the offline pipeline writes, except that it
grows over time. Every few minutes the whole session so far is run through
the normal extract and export stages and the outputs are rewritten, so the
list of questions for the expert is on screen before they leave.

Three layers, each usable on its own:

1. `LiveSession`: the growing store. `add_screenshot` keeps a frame only when
   the screen changed; `add_audio_chunk` transcribes a wav and appends the
   words; `save` writes `recording.json`.
2. `analyze`: the rolling analysis, plus `live_questions.md`, a short file
   with just the open questions, newest first.
3. `replay` and `capture`: two ways to feed the store. `replay` walks a
   folder of screenshots and a transcript file as if they were arriving live
   (this is what the tests drive); `capture` is the real thing, on macOS,
   Windows and Linux: `take_screenshot` grabs the screen with the `mss`
   library or the platform's own tool, and ffmpeg records the microphone
   through the input that platform has (avfoundation, dshow, pulse/alsa).
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from specto import transcript as transcript_module  # module attribute, so tests can monkeypatch transcribe
from specto.align import build_moments
from specto.diff import changed_region, crop_path_for, crop_region, crop_wanted
from specto.export import export_all
from specto.extract import ClaudeCaller, ModelCaller, extract
from specto.ingest import content_hash, dhash, ffmpeg_exe, hamming
from specto.model import Analysis, Keyframe, Recording, TranscriptSegment, Word
from specto.timefmt import mmss

MAX_WIDTH = 1280  # saved frames are scaled to at most this wide, the same as the offline ingest
SCREENSHOT_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
QUESTIONS_FILE = "live_questions.md"
QUESTIONS_HEADING = "Questions to ask before the call ends"
JPEG_QUALITY = 85  # for live screenshots, whichever backend takes them

# Screen grab backends. `mss` is a Python library (the `live` extra) that works on
# macOS, Windows and Linux under X11; the rest are the platforms' own commands.
SCREENCAPTURE = "/usr/sbin/screencapture"  # macOS
GRIM = "grim"  # Linux, Wayland
IMPORT = "import"  # Linux, X11 (ImageMagick)
POWERSHELL = "powershell"  # Windows
SCREENSHOT_BACKENDS = ("mss", "screencapture", "grim", "import", "powershell")
INSTALL_HINTS = {
    "mss": 'pip install "specto[live]"',
    "screencapture": "it ships with macOS in /usr/sbin",
    "grim": "install grim (apt install grim); it is for Wayland sessions",
    "import": "install ImageMagick (apt install imagemagick); it is for X11 sessions",
    "powershell": "PowerShell ships with Windows; check powershell.exe is on PATH",
}
SCREEN_HINTS = {
    "darwin": (
        "On macOS the terminal needs Screen Recording permission: System Settings > Privacy & Security > "
        "Screen Recording, switch on your terminal app, then start again."
    ),
    "win32": 'On Windows install the mss library (pip install "specto[live]") or make sure powershell.exe is on PATH; no permission setting is needed.',
    "linux": (
        'On Linux install the mss library for X11 (pip install "specto[live]"), grim for Wayland (apt install grim) '
        "or ImageMagick for the import command (apt install imagemagick)."
    ),
}
MIC_HINTS = {
    "darwin": (
        "On macOS the terminal needs Microphone permission: System Settings > Privacy & Security > Microphone, "
        "switch on your terminal app, then start again."
    ),
    "win32": (
        "On Windows allow desktop apps to use the microphone (Settings > Privacy & security > Microphone) and, "
        "if the wrong input was picked, pass --audio-device with a name from "
        "`ffmpeg -list_devices true -f dshow -i dummy`."
    ),
    "linux": (
        "On Linux check that PulseAudio or PipeWire is running (`pactl info`), or pass an ALSA device "
        "such as --audio-device hw:0."
    ),
}
# The macOS wording, kept under the old names.
SCREEN_PERMISSION_HINT = "specto could not take a screenshot. " + SCREEN_HINTS["darwin"]
MIC_PERMISSION_HINT = "specto could not record the microphone. " + MIC_HINTS["darwin"]

# The CLI's default microphone is avfoundation's ":0"; on the other platforms it
# just means "the default input" and is translated in `audio_input_args`.
DEFAULT_AUDIO_DEVICE = ":0"

_SHOT_TIME_RE = re.compile(r"(\d+(?:\.\d+)?)$")


# ------------------------------------------------------------------- the store


class LiveSession:
    """A `Recording` that grows while the call is on, plus its `frames/` folder.

    Screenshots come in through `add_screenshot`, which keeps only the ones
    that show a changed screen. Speech comes in through `add_audio_chunk`
    (a wav file, transcribed here) or `add_segments` (already-timed text).
    `save` rebuilds the moments and writes `recording.json`; it is cheap, so
    call it after every addition and a crash loses nothing. `load` picks a
    session up again from that file.

    All public methods take one lock, so a screenshot thread and an audio
    thread can share a session. Transcription itself runs outside the lock.
    """

    def __init__(
        self,
        out_dir: str | Path,
        hash_distance: int = 8,
        min_gap: float = 1.5,
        source: str = "live",
    ) -> None:
        self.out_dir = Path(out_dir)
        self.frames_dir = self.out_dir / "frames"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.hash_distance = hash_distance
        self.min_gap = min_gap
        self.recording = Recording(source=source, duration=0.0)
        self.latest_timestamp = 0.0
        # Rolling-analysis bookkeeping, all in recording time (seconds), not wall-clock.
        self.last_analysis_at: Optional[float] = None
        self.last_analysis_frames = 0
        self.last_analysis_segments = 0
        self.last_analysis: Optional[Analysis] = None
        self.analysis_rounds = 0
        self.screenshots_seen = 0
        self._last_hash: Optional[str] = None
        self._last_kept_time = float("-inf")
        self._lock = threading.RLock()

    # -- resume ---------------------------------------------------------------

    @classmethod
    def load(cls, out_dir: str | Path, hash_distance: int = 8, min_gap: float = 1.5) -> "LiveSession":
        """Reopen a session from its `recording.json` after a crash or a break.

        Frames, transcript and duration come back exactly as saved, and the
        next kept frame carries on the numbering. The analysis timer starts
        afresh, so the first `analyze` after a resume runs straight away.
        """
        out_dir = Path(out_dir)
        recording_json = out_dir / "recording.json"
        if not recording_json.exists():
            raise FileNotFoundError(f"no recording.json in {out_dir}; nothing to resume")
        session = cls(out_dir, hash_distance=hash_distance, min_gap=min_gap)
        session.recording = Recording.model_validate_json(recording_json.read_text(encoding="utf-8"))
        session.latest_timestamp = session.recording.duration
        if session.recording.keyframes:
            last = session.recording.keyframes[-1]
            session._last_hash = content_hash(out_dir / last.path)
            session._last_kept_time = last.timestamp
        session.screenshots_seen = len(session.recording.keyframes)
        return session

    # -- screen ---------------------------------------------------------------

    def add_screenshot(self, path_or_image: str | Path | Image.Image, timestamp: float) -> Optional[Keyframe]:
        """Offer one screenshot; keep it as the next keyframe if the screen changed.

        The picture is scaled to at most MAX_WIDTH wide and compared with the
        last kept frame using the same `content_hash` as the offline ingest.
        It is kept when the hashes differ by more than `hash_distance` bits
        and at least `min_gap` seconds have passed since the last kept frame;
        the first screenshot is always kept. A kept frame is saved as
        `frames/frame_NNNN.jpg`, gets its change box against the previous
        frame (and a close-up when the change is small), and is appended to
        `recording.keyframes`. Returns the Keyframe, or None when dropped.
        """
        with self._lock:
            self.screenshots_seen += 1
            self.latest_timestamp = max(self.latest_timestamp, timestamp)
            index = len(self.recording.keyframes)
            final = self.frames_dir / f"frame_{index:04d}.jpg"
            pending = self.frames_dir / "pending.jpg"
            _save_scaled(path_or_image, pending)
            digest = content_hash(pending)
            if self._last_hash is not None:
                if hamming(digest, self._last_hash) <= self.hash_distance:
                    pending.unlink()
                    return None
                if timestamp - self._last_kept_time < self.min_gap:
                    pending.unlink()
                    return None
            pending.rename(final)
            with Image.open(final) as img:
                width, height = img.size
            keyframe = Keyframe(
                index=index,
                timestamp=round(timestamp, 3),
                path=f"frames/{final.name}",
                phash=dhash(final),
                width=width,
                height=height,
            )
            if self.recording.keyframes:
                previous = self.out_dir / self.recording.keyframes[-1].path
                region = changed_region(previous, final)
                keyframe.change_from_previous = region
                if crop_wanted(region, width, height):
                    crop_region(final, region, self.out_dir / crop_path_for(keyframe.path))
            self.recording.keyframes.append(keyframe)
            self._last_hash = digest
            self._last_kept_time = timestamp
            return keyframe

    # -- speech ---------------------------------------------------------------

    def add_audio_chunk(self, wav_path: str | Path, start_time: float, model_size: str = "base") -> list[TranscriptSegment]:
        """Transcribe one audio chunk that started at `start_time` and append its words.

        Times inside the chunk count from zero, so every segment and word is
        shifted by `start_time` before it joins the recording. Transcription
        runs outside the lock because it takes seconds. Returns the segments
        as added.
        """
        segments = transcript_module.transcribe(wav_path, model_size=model_size)
        shifted = [_shift_segment(segment, start_time) for segment in segments]
        with self._lock:
            self.recording.segments.extend(shifted)
            self.recording.transcript_source = "whisper"
            self._note_segment_times(shifted)
        return shifted

    def add_segments(self, segments: list[TranscriptSegment]) -> None:
        """Append already-timed segments (replay from a transcript file)."""
        with self._lock:
            self.recording.segments.extend(segments)
            if self.recording.transcript_source == "none":
                self.recording.transcript_source = "file"
            self._note_segment_times(segments)

    def _note_segment_times(self, segments: list[TranscriptSegment]) -> None:
        for segment in segments:
            self.latest_timestamp = max(self.latest_timestamp, segment.end)

    # -- persistence ----------------------------------------------------------

    def save(self) -> Path:
        """Rebuild the moments, set the duration to the latest time seen, write `recording.json`."""
        with self._lock:
            recording = self.recording
            recording.duration = max(recording.duration, self.latest_timestamp)
            # build_moments prints a summary line; every few seconds that is
            # noise on a live console, so it is swallowed here.
            with contextlib.redirect_stdout(io.StringIO()):
                recording.moments = build_moments(recording.keyframes, recording.segments, recording.duration)
            path = self.out_dir / "recording.json"
            path.write_text(recording.model_dump_json(indent=2), encoding="utf-8")
            return path

    def snapshot(self) -> Recording:
        """A copy of the recording as it is now, safe to hand to a long analysis."""
        with self._lock:
            return self.recording.model_copy(deep=True)


def _save_scaled(path_or_image: str | Path | Image.Image, out_path: Path, max_width: int = MAX_WIDTH) -> None:
    """Write `path_or_image` as a JPEG no wider than `max_width` (aspect kept)."""
    if isinstance(path_or_image, Image.Image):
        image = path_or_image.convert("RGB")
    else:
        with Image.open(path_or_image) as img:
            image = img.convert("RGB")
    width, height = image.size
    if width > max_width:
        new_height = max(1, round(height * max_width / width))
        image = image.resize((max_width, new_height), Image.Resampling.LANCZOS)
    image.save(out_path, "JPEG", quality=90)


def _shift_segment(segment: TranscriptSegment, offset: float) -> TranscriptSegment:
    return TranscriptSegment(
        start=segment.start + offset,
        end=segment.end + offset,
        text=segment.text,
        speaker=segment.speaker,
        words=[Word(start=w.start + offset, end=w.end + offset, text=w.text) for w in segment.words],
    )


# ------------------------------------------------------------ rolling analysis


def analyze(
    session: LiveSession,
    caller: ModelCaller,
    every_seconds: float = 300,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> Optional[Analysis]:
    """Run the full analysis on the session so far, when it is due.

    It runs when there has been no analysis yet, when `every_seconds` of
    recording time have passed since the last one, or when `force` is set and
    something (a frame or a segment) has arrived since the last one. Each
    round saves the session, runs `specto.extract.extract` with `force=True`
    on the whole recording, exports the workbook and report, and rewrites
    `live_questions.md`. Returns the Analysis, or None when nothing ran.

    Cost: each round reuses the chunk readings from earlier rounds (extract's
    `reuse_readings`), so only the chunk still being filled and any chunk whose
    words changed are sent again, plus the one merge call. A ten-round call
    reads each full chunk once.
    """
    with session._lock:
        frames = len(session.recording.keyframes)
        segments = len(session.recording.segments)
        if frames == 0:
            return None
        new_content = frames > session.last_analysis_frames or segments > session.last_analysis_segments
        elapsed = None if session.last_analysis_at is None else session.latest_timestamp - session.last_analysis_at
        due = elapsed is None or elapsed >= every_seconds or (force and new_content)
        if not due:
            return None
        session.save()
        recording = session.snapshot()
        session.analysis_rounds += 1
        round_number = session.analysis_rounds

    log(f"live: analysis round {round_number} at {mmss(recording.duration)}, {frames} frames, {segments} segments")
    analysis = extract(recording, session.out_dir, caller=caller, force=True, reuse_readings=True, log=log)
    export_all(analysis, recording, session.out_dir)
    questions_path = write_live_questions(analysis, session.out_dir / QUESTIONS_FILE, recording.duration)
    log(f"live: {len(analysis.questions)} questions in {questions_path}")

    with session._lock:
        session.last_analysis = analysis
        session.last_analysis_at = session.latest_timestamp
        session.last_analysis_frames = frames
        session.last_analysis_segments = segments
    return analysis


def write_live_questions(analysis: Analysis, path: str | Path, duration: float) -> Path:
    """Write the open questions as a short Markdown file.

    Meant to sit open in a window during the call: one heading per question
    with its id and time, then the question and why it matters. Questions that
    hold up the most requirements come first, so the analyst asks those before
    the expert leaves; among equals the newest comes first.
    """
    path = Path(path)
    questions = sorted(
        analysis.questions,
        key=lambda q: (-len(q.blocks_requirement_ids), -q.timestamp, q.id),
    )
    lines = [
        f"# {QUESTIONS_HEADING}",
        "",
        f"Updated at {mmss(duration)} into the call. {len(questions)} open question{'s' if len(questions) != 1 else ''}, "
        "most blocking first, then newest first.",
        "",
    ]
    if not questions:
        lines.append("Nothing to ask yet.")
    for question in questions:
        blocks = f" (blocks {', '.join(question.blocks_requirement_ids)})" if question.blocks_requirement_ids else ""
        lines.append(f"## {question.id} at {mmss(question.timestamp)}")
        lines.append("")
        lines.append(question.question.strip() + blocks)
        lines.append("")
        lines.append(f"Why it matters: {question.why_it_matters.strip()}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------- replay


def shot_time(path: str | Path) -> float:
    """Seconds encoded in a screenshot file name: `shot_0012.5.png` -> 12.5, `shot_12.png` -> 12."""
    stem = Path(path).stem
    match = _SHOT_TIME_RE.search(stem)
    if not match:
        raise ValueError(f"screenshot name must end in its time in seconds, e.g. shot_12.5.png, not {Path(path).name!r}")
    return float(match.group(1))


def replay(
    out_dir: str | Path,
    frames_dir: str | Path,
    transcript_path: Optional[str | Path],
    caller: ModelCaller,
    speed: Optional[float] = None,
    every_seconds: float = 300,
    hash_distance: int = 8,
    min_gap: float = 1.5,
    log: Callable[[str], None] = print,
) -> LiveSession:
    """Feed saved screenshots and a transcript through the store as if live.

    Screenshots are the image files in `frames_dir` named with their time in
    seconds (`shot_0012.5.png` or `shot_12.png`). Transcript cues arrive at
    their end time, when they would have been fully spoken. Everything goes
    in time order; `analyze` runs each time the recording time crosses a
    multiple of `every_seconds`, and once more at the end. There is no real
    waiting unless `speed` is set (1.0 is real time, 10.0 is ten times
    faster); that is only for a demo. Returns the session, whose
    `last_analysis` is the final Analysis.
    """
    frames_dir = Path(frames_dir)
    shots = sorted(
        (p for p in frames_dir.iterdir() if p.suffix.lower() in SCREENSHOT_SUFFIXES),
        key=shot_time,
    )
    if not shots:
        raise FileNotFoundError(f"no screenshots (.png/.jpg) in {frames_dir}")
    segments = transcript_module.parse_transcript(transcript_path) if transcript_path else []

    events: list[tuple[float, int, object]] = [(shot_time(p), 0, p) for p in shots]
    events += [(s.end, 1, s) for s in segments]
    events.sort(key=lambda e: (e[0], e[1]))

    session = LiveSession(out_dir, hash_distance=hash_distance, min_gap=min_gap)
    log(f"live: replaying {len(shots)} screenshots and {len(segments)} transcript cues from {frames_dir}")
    next_boundary = every_seconds
    previous_time = events[0][0]
    for at, kind, item in events:
        if speed:
            time.sleep(max(0.0, (at - previous_time) / speed))
        previous_time = at
        if kind == 0:
            kept = session.add_screenshot(item, at)
            if kept is not None:
                log(f"live: kept frame {kept.index} at {mmss(at)}")
        else:
            session.add_segments([item])
        session.save()
        while at >= next_boundary:
            analyze(session, caller, every_seconds=every_seconds, force=True, log=log)
            next_boundary += every_seconds
    analyze(session, caller, every_seconds=every_seconds, force=True, log=log)
    log(f"live: done, {len(session.recording.keyframes)} frames kept, outputs in {session.out_dir}")
    return session


# --------------------------------------------------------- screen and mic


def normalize_platform(name: Optional[str] = None) -> str:
    """'darwin', 'win32' or 'linux' from a `sys.platform` or `platform.system()` spelling; None means this machine."""
    name = (name or sys.platform).lower()
    if name.startswith(("darwin", "mac")):
        return "darwin"
    if name.startswith(("win", "cygwin", "msys")):
        return "win32"
    return "linux"  # linux and the BSDs use the same tools


def wayland_session() -> bool:
    """True when this Linux session runs on Wayland, where mss and import cannot grab the screen."""
    return bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"


def screenshot_backends(platform: Optional[str] = None, wayland: Optional[bool] = None) -> list[str]:
    """The backends `take_screenshot` tries on `platform`, in order.

    mss comes first everywhere it can work; on a Linux Wayland session (found
    from the environment when `wayland` is None) grim comes first instead.
    """
    platform = normalize_platform(platform)
    if platform == "darwin":
        return ["mss", "screencapture"]
    if platform == "win32":
        return ["mss", "powershell"]
    if wayland is None:
        wayland = wayland_session()
    return ["grim", "mss", "import"] if wayland else ["mss", "grim", "import"]


def macos_screen_recording_allowed() -> Optional[bool]:
    """Whether this process has macOS Screen Recording permission; None when it cannot be asked.

    Uses CoreGraphics' CGPreflightScreenCaptureAccess. Without the permission
    a screen grab through mss still returns an image, but one showing only
    the wallpaper and the menu bar, which is worse than a clear failure.
    """
    if normalize_platform() != "darwin":
        return None
    try:
        import ctypes
        import ctypes.util

        lib = ctypes.util.find_library("CoreGraphics")
        if not lib:
            return None
        core_graphics = ctypes.CDLL(lib)
        core_graphics.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(core_graphics.CGPreflightScreenCaptureAccess())
    except Exception:
        return None


def _run_tool(argv: list[str], timeout: float = 30) -> tuple[bool, str]:
    """Run a screenshot command; (True, "") on exit 0, else (False, why) in one line."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, f"{argv[0]} not found"
    except subprocess.TimeoutExpired:
        return False, f"{argv[0]} did not finish in {timeout:g} seconds"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
    return True, ""


def _shot_mss(out_path: Path, display: int, platform: str, command: str) -> tuple[bool, str]:
    try:
        import mss  # optional: the `live` extra
    except ImportError:
        return False, "not installed"
    if platform == "darwin" and macos_screen_recording_allowed() is False:
        return False, "Screen Recording permission not granted (the grab would show only the wallpaper)"
    grabber = getattr(mss, "MSS", None) or mss.mss  # the factory is deprecated from mss 10
    try:
        with grabber() as sct:
            monitors = sct.monitors  # [0] is every monitor together, then one entry per monitor
            if display < 1 or display >= len(monitors):
                return False, f"no display {display}; this machine has {len(monitors) - 1}"
            shot = sct.grab(monitors[display])
            Image.frombytes("RGB", tuple(shot.size), shot.bgra, "raw", "BGRX").save(
                out_path, "JPEG", quality=JPEG_QUALITY
            )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}".strip(": ")
    return True, ""


def _shot_screencapture(out_path: Path, display: int, platform: str, command: str) -> tuple[bool, str]:
    return _run_tool([command, "-x", "-t", "jpg", "-D", str(display), str(out_path)])


def _shot_grim(out_path: Path, display: int, platform: str, command: str) -> tuple[bool, str]:
    # grim picks outputs by name, not number, so every monitor is grabbed together.
    return _run_tool([GRIM, "-t", "jpeg", "-q", str(JPEG_QUALITY), str(out_path)])


def _shot_import(out_path: Path, display: int, platform: str, command: str) -> tuple[bool, str]:
    return _run_tool([IMPORT, "-window", "root", "-quality", str(JPEG_QUALITY), str(out_path)])


def powershell_screenshot_script(out_path: Path, display: int) -> str:
    """One PowerShell line that saves screen `display` (1 = primary) as a JPEG with System.Drawing."""
    screen = (
        "[System.Windows.Forms.Screen]::PrimaryScreen"
        if display == 1
        else f"[System.Windows.Forms.Screen]::AllScreens[{display - 1}]"
    )
    path = str(out_path).replace("'", "''")
    return (
        "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
        f"$s = {screen}; if ($null -eq $s) {{ throw 'no display {display}' }}; "
        "$b = New-Object System.Drawing.Bitmap $s.Bounds.Width, $s.Bounds.Height; "
        "$g = [System.Drawing.Graphics]::FromImage($b); "
        "$g.CopyFromScreen($s.Bounds.Location, [System.Drawing.Point]::Empty, $s.Bounds.Size); "
        "$codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }; "
        "$params = New-Object System.Drawing.Imaging.EncoderParameters 1; "
        f"$params.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter ([System.Drawing.Imaging.Encoder]::Quality, [long]{JPEG_QUALITY}); "
        f"$b.Save('{path}', $codec, $params); $g.Dispose(); $b.Dispose()"
    )


def _shot_powershell(out_path: Path, display: int, platform: str, command: str) -> tuple[bool, str]:
    script = powershell_screenshot_script(out_path, display)
    return _run_tool([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script], timeout=60)


_SHOT_BACKENDS: dict[str, Callable[[Path, int, str, str], tuple[bool, str]]] = {
    "mss": _shot_mss,
    "screencapture": _shot_screencapture,
    "grim": _shot_grim,
    "import": _shot_import,
    "powershell": _shot_powershell,
}


def take_screenshot(
    out_path: str | Path,
    display: int = 1,
    command: str = SCREENCAPTURE,
    backend: str = "auto",
    platform: Optional[str] = None,
) -> tuple[bool, str]:
    """Grab one display to a JPEG. Returns (ok, error text).

    `backend="auto"` tries, in order, the `mss` library when it is installed
    (macOS, Windows, Linux under X11), then the platform's own tool: macOS
    `screencapture` (`command`), Linux `grim` (Wayland) or ImageMagick's
    `import` (X11), Windows PowerShell with System.Drawing. Pin one with
    `backend="mss"`, "screencapture", "grim", "import" or "powershell".
    `display` counts from 1; grim and import grab every monitor together.

    On failure the error names each backend that was tried and why it
    failed, then the permission or install hint for the platform. On macOS
    without Screen Recording permission `screencapture` exits 1 with "could
    not create image from display", and mss is not even tried, because it
    would return only the wallpaper. `platform` is for tests.
    """
    platform = normalize_platform(platform)
    out_path = Path(out_path)
    if backend == "auto":
        order = screenshot_backends(platform)
    elif backend in _SHOT_BACKENDS:
        order = [backend]
    else:
        raise ValueError(f"unknown screenshot backend {backend!r}; one of auto, {', '.join(SCREENSHOT_BACKENDS)}")
    failures = []
    for name in order:
        ok, error = _SHOT_BACKENDS[name](out_path, display, platform, command)
        if ok and not (out_path.exists() and out_path.stat().st_size > 0):
            ok, error = False, "no image written"
        if ok:
            return True, ""
        out_path.unlink(missing_ok=True)
        if error.endswith("not found") or error == "not installed":
            error += f" ({INSTALL_HINTS[name]})"
        failures.append(f"{name}: {error}")
    return False, "; ".join(failures) + ". " + SCREEN_HINTS[platform]


def audio_input_args(platform: Optional[str], device: str = DEFAULT_AUDIO_DEVICE) -> list[str]:
    """ffmpeg's input arguments for the microphone on `platform` (pure; no ffmpeg is run).

    macOS: `-f avfoundation -i :0` (an avfoundation index; `:0` is the
    default input). Windows: `-f dshow -i audio=<name>`; dshow needs a real
    device name, so the default marker ":0" is refused here and resolved by
    `start_audio_recorder` from ffmpeg's own device list. Linux: `-f pulse
    -i default` (PulseAudio or PipeWire), or `-f alsa` when the device
    looks like an ALSA name such as `hw:0`.
    """
    platform = normalize_platform(platform)
    device = device or DEFAULT_AUDIO_DEVICE
    if platform == "darwin":
        return ["-f", "avfoundation", "-i", device]
    if platform == "win32":
        if device == DEFAULT_AUDIO_DEVICE:
            raise ValueError(
                "ffmpeg's dshow input needs a device name; pass one from "
                "`ffmpeg -list_devices true -f dshow -i dummy` or let start_audio_recorder pick the first"
            )
        return ["-f", "dshow", "-i", f"audio={device}"]
    if device == DEFAULT_AUDIO_DEVICE:
        device = "default"
    alsa = device.startswith(("hw:", "plughw:", "sysdefault", "dsnoop", "dmix"))
    return ["-f", "alsa" if alsa else "pulse", "-i", device]


_DSHOW_DEVICE_RE = re.compile(r'^\[dshow @ [^\]]*\]\s*"(?P<name>[^"]+)"(?:\s*\((?P<kind>[^)]*)\))?\s*$')


def parse_dshow_devices(listing: str) -> list[str]:
    """Audio device names from `ffmpeg -list_devices true -f dshow -i dummy` (it prints to stderr).

    Reads both spellings: ffmpeg 5 and newer tag each line with "(audio)" or
    "(video)"; older builds print a "DirectShow audio devices" heading first.
    """
    names = []
    in_audio_section = False
    for line in listing.splitlines():
        if "DirectShow audio devices" in line:
            in_audio_section = True
            continue
        if "DirectShow video devices" in line:
            in_audio_section = False
            continue
        match = _DSHOW_DEVICE_RE.match(line.strip())
        if not match:
            continue
        kind = match.group("kind")
        if (kind is None and in_audio_section) or (kind is not None and "audio" in kind):
            names.append(match.group("name"))
    return names


def list_dshow_audio_devices() -> list[str]:
    """Names of the microphones ffmpeg's dshow input sees, first one first (Windows only)."""
    try:
        result = subprocess.run(
            [ffmpeg_exe(), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return []
    return parse_dshow_devices(result.stderr + result.stdout)


def resolve_audio_device(
    platform: Optional[str] = None,
    device: str = DEFAULT_AUDIO_DEVICE,
    log: Callable[[str], None] = print,
) -> str:
    """The device name ffmpeg gets; on Windows the default marker becomes the first dshow microphone.

    Raises RuntimeError when Windows has no dshow audio device to pick.
    """
    if normalize_platform(platform) != "win32" or device != DEFAULT_AUDIO_DEVICE:
        return device
    devices = list_dshow_audio_devices()
    if not devices:
        raise RuntimeError("ffmpeg lists no dshow audio device. " + MIC_HINTS["win32"])
    log(
        f"live: no --audio-device given, using the first microphone ffmpeg lists: {devices[0]!r} "
        "(`ffmpeg -list_devices true -f dshow -i dummy` shows the others)"
    )
    return devices[0]


def start_audio_recorder(
    out_path: str | Path,
    seconds: float,
    device: str = DEFAULT_AUDIO_DEVICE,
    platform: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> subprocess.Popen:
    """Start ffmpeg recording `seconds` of the microphone to a 16 kHz mono wav; returns the process.

    The input is picked for the platform by `audio_input_args` (avfoundation,
    dshow, pulse or alsa). On Windows with no device named, the first
    microphone in ffmpeg's dshow list is used and `log` says which.
    `platform` is for tests.
    """
    platform = normalize_platform(platform)
    device = resolve_audio_device(platform, device, log)
    return subprocess.Popen(
        [
            ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            *audio_input_args(platform, device),
            "-t", f"{seconds:g}", "-ac", "1", "-ar", "16000",
            str(out_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def _audio_loop(
    session: LiveSession,
    stop: threading.Event,
    started: float,
    chunk_seconds: float,
    device: str,
    whisper_model: str,
    log: Callable[[str], None],
) -> None:
    """Record the microphone chunk after chunk; transcribe each while the next records.

    The next chunk's recorder is started before the finished chunk is
    transcribed, so speech is not lost while whisper is busy. On the first
    failure (no permission, no such device) the hint is logged and the loop
    ends; the screenshots carry on without audio.
    """
    audio_dir = session.out_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    mic_hint = "specto could not record the microphone. " + MIC_HINTS[normalize_platform()]
    number = 0
    chunk_path = audio_dir / f"chunk_{number:04d}.wav"
    chunk_start = time.monotonic() - started
    try:
        recorder = start_audio_recorder(chunk_path, chunk_seconds, device, log=log)
    except (RuntimeError, ValueError, OSError) as exc:
        log(f"{mic_hint} ({exc})")
        return
    while True:
        while recorder.poll() is None:
            if stop.is_set():
                recorder.terminate()  # ffmpeg finishes the wav cleanly on SIGTERM
                try:
                    recorder.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    recorder.kill()
                break
            time.sleep(0.2)
        error = (recorder.stderr.read() if recorder.stderr else "").strip()
        if not chunk_path.exists() or chunk_path.stat().st_size <= 44:  # a wav header alone is 44 bytes
            log(f"{mic_hint} (ffmpeg said: {error or 'nothing recorded'})")
            return
        finished_path, finished_start = chunk_path, chunk_start
        if not stop.is_set():
            number += 1
            chunk_path = audio_dir / f"chunk_{number:04d}.wav"
            chunk_start = time.monotonic() - started
            recorder = start_audio_recorder(chunk_path, chunk_seconds, device, log=log)
        try:
            added = session.add_audio_chunk(finished_path, finished_start, model_size=whisper_model)
            session.save()
            log(f"live: audio chunk {finished_path.name} from {mmss(finished_start)}: {len(added)} segments")
        except Exception as exc:  # a bad chunk must not end the call
            log(f"live: could not transcribe {finished_path.name}: {exc}")
        if stop.is_set():
            return


def capture(
    out_dir: str | Path,
    interval: float = 3.0,
    audio_chunk_seconds: float = 30,
    every_seconds: float = 300,
    caller: Optional[ModelCaller] = None,
    display: int = 1,
    audio_device: str = DEFAULT_AUDIO_DEVICE,
    whisper_model: str = "base",
    log: Callable[[str], None] = print,
) -> LiveSession:
    """Capture the screen and the microphone until Ctrl-C, analysing as it goes.

    The main thread takes a screenshot every `interval` seconds with
    `take_screenshot` and offers it to the session; one background thread
    records the microphone in `audio_chunk_seconds` pieces with ffmpeg and
    transcribes each. `analyze` is tried after every screenshot and runs
    itself every `every_seconds`. Ctrl-C stops both, runs one last analysis
    and prints where the outputs are. If `out_dir` already holds a
    `recording.json` the session is resumed and its clock carries on from
    the saved duration.

    Works on macOS, Windows and Linux; see `take_screenshot` for the screen
    backends and `audio_input_args` for the microphone input. On macOS the
    terminal needs Screen Recording and Microphone permission; without
    either, one clear line says which setting to switch on. `audio_device`
    is ":0" for the default microphone; on macOS it is an avfoundation index
    (`ffmpeg -f avfoundation -list_devices true -i ""` lists them), on
    Windows a dshow name, on Linux a PulseAudio source or ALSA device.
    """
    out_dir = Path(out_dir)
    if (out_dir / "recording.json").exists():
        session = LiveSession.load(out_dir)
        log(f"live: resuming {out_dir} at {mmss(session.recording.duration)} with {len(session.recording.keyframes)} frames")
    else:
        session = LiveSession(out_dir)
    caller = caller or ClaudeCaller()
    started = time.monotonic() - session.recording.duration
    stop = threading.Event()
    audio_thread = threading.Thread(
        target=_audio_loop,
        args=(session, stop, started, audio_chunk_seconds, audio_device, whisper_model, log),
        name="specto-audio",
        daemon=True,
    )
    audio_thread.start()
    log(f"live: capturing display {display} every {interval:g}s, audio in {audio_chunk_seconds:g}s chunks; Ctrl-C to stop")
    shot_path = session.frames_dir / "live_shot.jpg"
    try:
        while True:
            tick = time.monotonic()
            now = tick - started
            ok, error = take_screenshot(shot_path, display=display)
            if not ok:
                log(f"specto could not take a screenshot: {error}")
                break
            kept = session.add_screenshot(shot_path, round(now, 3))
            shot_path.unlink(missing_ok=True)
            session.save()
            if kept is not None:
                log(f"live: kept frame {kept.index} at {mmss(now)}")
            analyze(session, caller, every_seconds=every_seconds, log=log)
            time.sleep(max(0.0, interval - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        log("live: stopping")
    finally:
        stop.set()
        audio_thread.join(timeout=audio_chunk_seconds + 15)
        session.save()
        analyze(session, caller, every_seconds=every_seconds, force=True, log=log)
        log(
            f"live: {len(session.recording.keyframes)} frames, {len(session.recording.segments)} segments, "
            f"{mmss(session.recording.duration)} of call. Outputs in {out_dir}: recording.json, analysis.json, "
            f"analysis.xlsx, report.md, {QUESTIONS_FILE}"
        )
    return session
