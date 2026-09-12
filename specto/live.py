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
   (this is what the tests drive); `capture` is the real thing on macOS,
   using `screencapture` for the screen and ffmpeg's avfoundation input for
   the microphone.
"""
from __future__ import annotations

import contextlib
import io
import re
import subprocess
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
SCREENCAPTURE = "/usr/sbin/screencapture"
SCREENSHOT_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
QUESTIONS_FILE = "live_questions.md"
QUESTIONS_HEADING = "Questions to ask before the call ends"

SCREEN_PERMISSION_HINT = (
    "specto could not take a screenshot. On macOS the terminal needs Screen Recording permission: "
    "System Settings > Privacy & Security > Screen Recording, switch on your terminal app, then start again."
)
MIC_PERMISSION_HINT = (
    "specto could not record the microphone. On macOS the terminal needs Microphone permission: "
    "System Settings > Privacy & Security > Microphone, switch on your terminal app, then start again."
)

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

    Cost: every round rereads every frame from the start, because extract has
    no notion of "the frames since last time". A ten-round call therefore
    reads the first frames ten times. Keeping the chunk readings from earlier
    rounds and reading only the new frames is the obvious next optimisation.
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
    analysis = extract(recording, session.out_dir, caller=caller, force=True, log=log)
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
    """Write the open questions, newest first, as a short Markdown file.

    Meant to sit open in a window during the call: one heading per question
    with its id and time, then the question and why it matters.
    """
    path = Path(path)
    questions = sorted(analysis.questions, key=lambda q: (q.timestamp, q.id), reverse=True)
    lines = [
        f"# {QUESTIONS_HEADING}",
        "",
        f"Updated at {mmss(duration)} into the call. {len(questions)} open question{'s' if len(questions) != 1 else ''}, newest first.",
        "",
    ]
    if not questions:
        lines.append("Nothing to ask yet.")
    for question in questions:
        lines.append(f"## {question.id} at {mmss(question.timestamp)}")
        lines.append("")
        lines.append(question.question.strip())
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


# ------------------------------------------------------------- macOS capture


def take_screenshot(
    out_path: str | Path, display: int = 1, command: str = SCREENCAPTURE
) -> tuple[bool, str]:
    """Grab one display to a JPEG with macOS `screencapture`. Returns (ok, error text).

    Without Screen Recording permission the command exits 1 with
    "could not create image from display" and writes nothing, which is what
    happens in a sandbox too; both come back as ok=False.
    """
    out_path = Path(out_path)
    try:
        result = subprocess.run(
            [command, "-x", "-t", "jpg", "-D", str(display), str(out_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return False, f"{command} not found (live capture only works on macOS)"
    except subprocess.TimeoutExpired:
        return False, f"{command} did not finish in 30 seconds"
    if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        return False, (result.stderr or result.stdout or "no image written").strip()
    return True, ""


def start_audio_recorder(out_path: str | Path, seconds: float, device: str = ":0") -> subprocess.Popen:
    """Start ffmpeg recording `seconds` of the microphone to a 16 kHz mono wav; returns the process."""
    return subprocess.Popen(
        [
            ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "avfoundation", "-i", device,
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
    number = 0
    chunk_path = audio_dir / f"chunk_{number:04d}.wav"
    chunk_start = time.monotonic() - started
    recorder = start_audio_recorder(chunk_path, chunk_seconds, device)
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
            log(f"{MIC_PERMISSION_HINT} (ffmpeg said: {error or 'nothing recorded'})")
            return
        finished_path, finished_start = chunk_path, chunk_start
        if not stop.is_set():
            number += 1
            chunk_path = audio_dir / f"chunk_{number:04d}.wav"
            chunk_start = time.monotonic() - started
            recorder = start_audio_recorder(chunk_path, chunk_seconds, device)
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
    audio_device: str = ":0",
    whisper_model: str = "base",
    log: Callable[[str], None] = print,
) -> LiveSession:
    """Capture the screen and the microphone on macOS until Ctrl-C, analysing as it goes.

    The main thread takes a screenshot every `interval` seconds with
    `screencapture` and offers it to the session; one background thread
    records the microphone in `audio_chunk_seconds` pieces with ffmpeg and
    transcribes each. `analyze` is tried after every screenshot and runs
    itself every `every_seconds`. Ctrl-C stops both, runs one last analysis
    and prints where the outputs are. If `out_dir` already holds a
    `recording.json` the session is resumed and its clock carries on from
    the saved duration.

    Needs macOS Screen Recording and Microphone permission for the terminal;
    without either, one clear line says which setting to switch on.
    `audio_device` is ffmpeg's avfoundation audio index (":0" is the default
    input; `ffmpeg -f avfoundation -list_devices true -i ""` lists them).
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
                log(f"{SCREEN_PERMISSION_HINT} ({error})")
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
