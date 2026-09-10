"""Stage 1: turn a recording into `recording.json`.

Three jobs: find out how long the video is, pull one still image per screen
change, and line the transcript up against those images. ffmpeg does the video
work; it comes bundled with imageio-ffmpeg so nothing needs installing. There
is no ffprobe in that bundle, so duration is read from ffmpeg's own output.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

import imageio_ffmpeg
from PIL import Image

from specto.align import build_moments
from specto.model import Keyframe, Recording, TranscriptSegment
from specto.transcript import parse_transcript, transcribe

DUPLICATE_DISTANCE = 6  # Hamming distance on a 64-bit hash at or below this means "same picture"

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_TIME_RE = re.compile(r"time=\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_PTS_TIME_RE = re.compile(r"pts_time:\s*(-?\d+(?:\.\d+)?)")


def ffmpeg_exe() -> str:
    """Path to the bundled ffmpeg binary (not on PATH, so ask imageio-ffmpeg)."""
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run_ffmpeg(args: list[str]) -> str:
    """Run ffmpeg and return its stderr, which is where it reports everything."""
    result = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostdin", *args],
        capture_output=True,
        text=True,
        errors="replace",
    )
    return result.stderr


def probe_duration(video_path: str | Path) -> float:
    """Length of the video in seconds.

    First choice is the `Duration:` line ffmpeg prints when it opens the file.
    Some containers (webm from a browser recorder, cut streams) report N/A, so
    the fallback decodes the whole file to nowhere and reads the last `time=`.
    """
    video_path = str(video_path)
    stderr = _run_ffmpeg(["-i", video_path])
    match = _DURATION_RE.search(stderr)
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    stderr = _run_ffmpeg(["-i", video_path, "-f", "null", "-"])
    times = _TIME_RE.findall(stderr)
    if not times:
        raise RuntimeError(f"ffmpeg could not read a duration for {video_path}")
    hours, minutes, seconds = times[-1]
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def dhash(image_path: str | Path) -> str:
    """Difference hash of an image as 16 hex characters.

    Shrink to 9x8 greyscale and record whether each pixel is brighter than its
    right-hand neighbour: 64 bits that describe the layout, not the colours or
    the size. Two frames of the same screen get almost the same bits.
    """
    with Image.open(image_path) as img:
        small = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = small.tobytes()  # mode "L": one byte per pixel, row by row
    value = 0
    for row in range(8):
        for col in range(8):
            value = (value << 1) | int(pixels[row * 9 + col] < pixels[row * 9 + col + 1])
    return f"{value:016x}"


def hamming(hash_a: str, hash_b: str) -> int:
    """How many bits differ between two hex hashes."""
    return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")


def _grab_frames(video_path: str, frames_dir: Path, select_expr: str, max_width: int) -> list[tuple[Path, float]]:
    """Run ffmpeg once with a `select` filter and return (file, timestamp) pairs.

    `showinfo` sits right after `select`, so it prints one line per kept frame
    and the n-th line belongs to the n-th file ffmpeg writes. Scaling comes
    after both so the timestamps are the source frame's own.
    """
    for old in frames_dir.glob("raw_*.jpg"):
        old.unlink()
    filters = f"select='{select_expr}',showinfo,scale=w='min(iw,{max_width})':h=-2"
    stderr = _run_ffmpeg(
        [
            "-i", video_path,
            "-vf", filters,
            "-fps_mode", "vfr",
            "-q:v", "3",
            "-y",
            str(frames_dir / "raw_%04d.jpg"),
        ]
    )
    timestamps = [float(m.group(1)) for line in stderr.splitlines() if "showinfo" in line for m in [_PTS_TIME_RE.search(line)] if m]
    files = sorted(frames_dir.glob("raw_*.jpg"))
    if len(files) != len(timestamps):
        # Should not happen, but if ffmpeg logs differ from what it wrote, trust the files.
        print(f"ingest: warning, {len(files)} frame files but {len(timestamps)} showinfo lines; pairing the shortest run")
    return list(zip(files, timestamps))


def extract_keyframes(
    video_path: str | Path,
    out_dir: str | Path,
    scene_threshold: float = 0.3,
    min_gap: float = 1.5,
    max_frames: int = 120,
    max_width: int = 1280,
    fallback_interval: float = 10.0,
) -> list[Keyframe]:
    """Write one JPEG per screen change to `out_dir/frames/` and describe each.

    Steps, in order: ffmpeg scene detection (frame 0 always kept); drop frames
    whose picture hash is within DUPLICATE_DISTANCE of the previous kept one;
    drop frames closer than `min_gap` seconds to the previous kept one; if still
    more than `max_frames`, keep an even spread over time. A recording of a
    screen that never changes gives fewer than three scene frames, so then we
    sample one frame every `fallback_interval` seconds instead (no duplicate
    removal, because those frames are meant to look alike).
    """
    video_path = str(video_path)
    out_dir = Path(out_dir)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    scene_expr = f"gt(scene,{scene_threshold})+eq(n,0)"
    grabbed = _grab_frames(video_path, frames_dir, scene_expr, max_width)
    print(f"ingest: scene detection found {len(grabbed)} frames (threshold {scene_threshold})")

    if len(grabbed) < 3:
        sample_expr = f"isnan(prev_selected_t)+gte(t-prev_selected_t,{fallback_interval})"
        grabbed = _grab_frames(video_path, frames_dir, sample_expr, max_width)
        print(f"ingest: static screen, sampled {len(grabbed)} frames every {fallback_interval:g}s instead")
        kept = list(grabbed)
    else:
        kept = []
        duplicates = 0
        too_close = 0
        previous_hash: Optional[str] = None
        previous_time = float("-inf")
        for path, timestamp in grabbed:
            digest = dhash(path)
            if previous_hash is not None and hamming(digest, previous_hash) <= DUPLICATE_DISTANCE:
                duplicates += 1
                continue
            if timestamp - previous_time < min_gap:
                too_close += 1
                continue
            kept.append((path, timestamp))
            previous_hash = digest
            previous_time = timestamp
        print(f"ingest: removed {duplicates} near-duplicates and {too_close} frames inside the {min_gap:g}s gap")

    if len(kept) > max_frames:
        step = (len(kept) - 1) / (max_frames - 1) if max_frames > 1 else len(kept)
        chosen = sorted({round(i * step) for i in range(max_frames)})
        kept = [kept[i] for i in chosen]
        print(f"ingest: thinned to {len(kept)} frames spread evenly over time (max {max_frames})")

    keep_set = {path for path, _ in kept}
    for path, _ in grabbed:
        if path not in keep_set:
            path.unlink()
    for old in frames_dir.glob("frame_*.jpg"):
        old.unlink()

    keyframes: list[Keyframe] = []
    for index, (path, timestamp) in enumerate(kept):
        final = frames_dir / f"frame_{index:04d}.jpg"
        path.rename(final)
        with Image.open(final) as img:
            width, height = img.size
        keyframes.append(
            Keyframe(
                index=index,
                timestamp=round(timestamp, 3),
                path=f"frames/{final.name}",
                phash=dhash(final),
                width=width,
                height=height,
            )
        )
    print(f"ingest: {len(keyframes)} keyframes written to {frames_dir}")
    return keyframes


def ingest(
    video_path: str | Path,
    out_dir: str | Path,
    transcript_path: Optional[str | Path] = None,
    whisper_model: Optional[str] = "base",
    force: bool = False,
    **keyframe_kwargs,
) -> Recording:
    """Run stage 1 end to end and write `out_dir/recording.json`.

    Transcript comes from `transcript_path` when given, otherwise from
    faster-whisper with `whisper_model`; pass `whisper_model=None` to go without
    a transcript. Extra keyword arguments go to `extract_keyframes`. If the
    output already exists it is loaded and returned, unless `force` is set.
    """
    video_path = Path(video_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    recording_json = out_dir / "recording.json"

    if recording_json.exists() and not force:
        print(f"ingest: {recording_json} already exists, loading it (use force=True to redo)")
        return Recording.model_validate_json(recording_json.read_text(encoding="utf-8"))

    duration = probe_duration(video_path)
    print(f"ingest: {video_path.name} is {duration:.1f}s long")
    keyframes = extract_keyframes(video_path, out_dir, **keyframe_kwargs)

    segments: list[TranscriptSegment]
    if transcript_path is not None:
        segments = parse_transcript(transcript_path)
        transcript_source = "file"
    elif whisper_model:
        segments = transcribe(video_path, model_size=whisper_model)
        transcript_source = "whisper"
    else:
        segments = []
        transcript_source = "none"
        print("ingest: no transcript, keyframes only")

    moments = build_moments(keyframes, segments, duration)
    recording = Recording(
        source=str(video_path),
        duration=duration,
        keyframes=keyframes,
        segments=segments,
        moments=moments,
        transcript_source=transcript_source,
    )
    recording_json.write_text(recording.model_dump_json(indent=2), encoding="utf-8")
    print(f"ingest: wrote {recording_json}")
    return recording
