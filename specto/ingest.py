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


CONTENT_GRID = 24  # luma dhash grid for content_hash: 24x24 cells, 576 bits
COLOUR_GRID = 4  # colour signature grid: 4x4 cells, 6 bits each, 96 bits
COLOUR_MARGIN = 8  # one channel must beat another by this much (0-255) to count as a different hue


def content_hash(image_path: str | Path) -> str:
    """A 672-bit hash of what is on a screen: layout, text and colour. 168 hex chars.

    Two parts, concatenated:

    1. A difference hash of the greyscale picture on a 24x24 grid (576 bits),
       the same idea as `dhash` but three times finer in each direction. The
       8x8 version describes a page layout well but cannot see three typed
       values in a form: measured on the test fixture they flip 3 bits, the
       same as encoder noise. At 24x24 they flip about 16 bits while noise
       between two samples of a static screen stays at 0 to 2 and a moving
       mouse cursor at about 4.

    2. A colour signature on a 4x4 grid (96 bits): for each cell, for each
       pair of channels (red/green, green/blue, red/blue), two bits saying
       whether the first channel is clearly above the second and whether the
       second is clearly above the first, "clearly" meaning by more than
       COLOUR_MARGIN. A mid-green page turning mid-red flips four bits in
       every cell, 64 in all; a grey or white page stays at all zeros, so
       JPEG chroma noise cannot flip anything.

    Why not a dhash per colour channel: with white text on a coloured
    background every channel has the same "text brighter than background"
    gradients, so green and red pages hash the same. The colour signature
    compares channels against each other instead, which is what hue is.

    Distances are Hamming distances (`hamming`). Same-screen noise is 0 to 2
    bits, so a threshold near 8 separates "same screen" from "something
    changed" with room on both sides.
    """
    with Image.open(image_path) as img:
        rgb = img.convert("RGB")
    grid = CONTENT_GRID
    grey = rgb.convert("L").resize((grid + 1, grid), Image.Resampling.BOX).tobytes()
    value = 0
    for row in range(grid):
        for col in range(grid):
            value = (value << 1) | int(grey[row * (grid + 1) + col] < grey[row * (grid + 1) + col + 1])
    colours = rgb.resize((COLOUR_GRID, COLOUR_GRID), Image.Resampling.BOX).tobytes()  # RGB triples, row by row
    for cell in range(COLOUR_GRID * COLOUR_GRID):
        r, g, b = colours[cell * 3 : cell * 3 + 3]
        for first, second in ((r, g), (g, b), (r, b)):
            value = (value << 2) | (int(first > second + COLOUR_MARGIN) << 1) | int(second > first + COLOUR_MARGIN)
    total_bits = grid * grid + COLOUR_GRID * COLOUR_GRID * 6
    return f"{value:0{total_bits // 4}x}"


def _clear(frames_dir: Path, pattern: str) -> None:
    for old in frames_dir.glob(pattern):
        old.unlink()


def _grab_frames(video_path: str, frames_dir: Path, select_expr: str, max_width: int) -> list[tuple[Path, float]]:
    """Run ffmpeg once with a `select` filter and return (file, timestamp) pairs.

    `showinfo` sits right after `select`, so it prints one line per kept frame
    and the n-th line belongs to the n-th file ffmpeg writes. Scaling comes
    after both so the timestamps are the source frame's own.
    """
    _clear(frames_dir, "raw_*.jpg")
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


def _sample_frames(video_path: str, frames_dir: Path, sample_fps: float, max_width: int) -> list[tuple[Path, float]]:
    """Write one frame every 1/sample_fps seconds and return (file, timestamp) pairs.

    ffmpeg's `fps` filter puts its output on an exact grid, so the n-th file
    (counted from 0) is the frame at n / sample_fps seconds; no log parsing
    needed. A one-hour recording at 1 fps is 3,600 small JPEGs on disk, which
    is fine; only their paths are held in memory.
    """
    _clear(frames_dir, "sample_*.jpg")
    filters = f"fps={sample_fps:g},scale=w='min(iw,{max_width})':h=-2"
    _run_ffmpeg(
        [
            "-i", video_path,
            "-vf", filters,
            "-q:v", "3",
            "-y",
            str(frames_dir / "sample_%05d.jpg"),
        ]
    )
    files = sorted(frames_dir.glob("sample_*.jpg"))
    return [(path, index / sample_fps) for index, path in enumerate(files)]


def _detect_scene(
    video_path: str, frames_dir: Path, scene_threshold: float, min_gap: float, max_width: int
) -> tuple[list[tuple[Path, float]], int]:
    """ffmpeg scene detection (brightness change), then near-duplicate and gap removal.

    Returns the kept (file, timestamp) pairs and how many frames the detector
    found before thinning, which decides whether the screen counts as static.
    Rejected files are deleted as we go.
    """
    scene_expr = f"gt(scene,{scene_threshold})+eq(n,0)"
    grabbed = _grab_frames(video_path, frames_dir, scene_expr, max_width)
    print(f"ingest: scene detection found {len(grabbed)} frames (threshold {scene_threshold})")
    if len(grabbed) < 3:
        return grabbed, len(grabbed)

    kept: list[tuple[Path, float]] = []
    duplicates = 0
    too_close = 0
    previous_hash: Optional[str] = None
    previous_time = float("-inf")
    for path, timestamp in grabbed:
        digest = dhash(path)
        if previous_hash is not None and hamming(digest, previous_hash) <= DUPLICATE_DISTANCE:
            duplicates += 1
            path.unlink()
            continue
        if timestamp - previous_time < min_gap:
            too_close += 1
            path.unlink()
            continue
        kept.append((path, timestamp))
        previous_hash = digest
        previous_time = timestamp
    print(f"ingest: removed {duplicates} near-duplicates and {too_close} frames inside the {min_gap:g}s gap")
    return kept, len(grabbed)


def _detect_hash(
    video_path: str, frames_dir: Path, sample_fps: float, hash_distance: int, min_gap: float, max_width: int
) -> tuple[list[tuple[Path, float]], int]:
    """Sample the video on a fixed grid and keep each frame that differs from the last kept one.

    Walks the samples in time order, one image in memory at a time. A sample
    is kept when its `content_hash` is more than `hash_distance` bits from
    the last kept frame's hash and it is at least `min_gap` seconds later;
    everything else is deleted straight away. Comparing with the last *kept*
    frame, not the previous sample, means slow changes (text typed one
    character a second) still add up to a new frame.

    Returns the kept (file, timestamp) pairs and how many samples differed
    from the last kept frame, gap or no gap: fewer than three of those means
    the screen never really changed.
    """
    samples = _sample_frames(video_path, frames_dir, sample_fps, max_width)
    kept: list[tuple[Path, float]] = []
    changed = 0
    previous_hash: Optional[str] = None
    previous_time = float("-inf")
    for path, timestamp in samples:
        digest = content_hash(path)
        if previous_hash is not None and hamming(digest, previous_hash) <= hash_distance:
            path.unlink()
            continue
        changed += 1
        if timestamp - previous_time < min_gap:
            path.unlink()
            continue
        kept.append((path, timestamp))
        previous_hash = digest
        previous_time = timestamp
    print(
        f"ingest: hash detection looked at {len(samples)} samples ({sample_fps:g} per second) "
        f"and kept {len(kept)} (distance over {hash_distance})"
    )
    return kept, changed


def extract_keyframes(
    video_path: str | Path,
    out_dir: str | Path,
    detect: str = "hash",
    sample_fps: float = 1.0,
    hash_distance: int = 8,
    scene_threshold: float = 0.3,
    min_gap: float = 1.5,
    max_frames: int = 120,
    max_width: int = 1280,
    fallback_interval: float = 10.0,
) -> list[Keyframe]:
    """Write one JPEG per screen change to `out_dir/frames/` and describe each.

    Two detectors, chosen with `detect`:

    - "hash" (default): take one frame every 1/`sample_fps` seconds, hash
      each with `content_hash` (layout, text and colour) and keep a frame when
      it is more than `hash_distance` bits from the last kept one. Sees a
      green page turn red and text typed into a form, which the scene detector
      cannot.
    - "scene": ffmpeg scene detection, which scores brightness change only,
      with `scene_threshold`; frames whose 8x8 `dhash` is within
      DUPLICATE_DISTANCE of the previous kept one are dropped.

    Both then drop frames closer than `min_gap` seconds to the previous kept
    one (frame 0 is always kept) and, if still more than `max_frames`, keep an
    even spread over time. A recording of a screen that never changes gives
    fewer than three frames in either mode, so then we sample one frame every
    `fallback_interval` seconds instead (no duplicate removal, because those
    frames are meant to look alike). Frames end up as frames/frame_NNNN.jpg,
    numbered contiguously from 0 in time order; everything else is deleted.
    """
    video_path = str(video_path)
    out_dir = Path(out_dir)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for pattern in ("raw_*.jpg", "sample_*.jpg"):  # leftovers from a run that crashed part-way
        _clear(frames_dir, pattern)

    if detect == "hash":
        kept, detected = _detect_hash(video_path, frames_dir, sample_fps, hash_distance, min_gap, max_width)
    elif detect == "scene":
        kept, detected = _detect_scene(video_path, frames_dir, scene_threshold, min_gap, max_width)
    else:
        raise ValueError(f"detect must be 'hash' or 'scene', not {detect!r}")

    if detected < 3:
        for path, _ in kept:
            path.unlink()
        sample_expr = f"isnan(prev_selected_t)+gte(t-prev_selected_t,{fallback_interval})"
        kept = _grab_frames(video_path, frames_dir, sample_expr, max_width)
        print(f"ingest: static screen, sampled {len(kept)} frames every {fallback_interval:g}s instead")

    if len(kept) > max_frames:
        step = (len(kept) - 1) / (max_frames - 1) if max_frames > 1 else len(kept)
        chosen = sorted({round(i * step) for i in range(max_frames)})
        chosen_set = set(chosen)
        for i, (path, _) in enumerate(kept):
            if i not in chosen_set:
                path.unlink()
        kept = [kept[i] for i in chosen]
        print(f"ingest: thinned to {len(kept)} frames spread evenly over time (max {max_frames})")

    _clear(frames_dir, "frame_*.jpg")
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
    a transcript. Extra keyword arguments go to `extract_keyframes` (`detect`,
    `sample_fps`, `hash_distance`, `scene_threshold`, `min_gap`, `max_frames`,
    `max_width`, `fallback_interval`). If the
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
