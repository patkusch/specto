"""Stage 1: turn a recording into `recording.json`.

Three jobs: find out how long the video is, pull one still image per screen
change, and line the transcript up against those images. ffmpeg does the video
work; it comes bundled with imageio-ffmpeg so nothing needs installing. There
is no ffprobe in that bundle, so duration is read from ffmpeg's own output.
"""
from __future__ import annotations

import heapq
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import imageio_ffmpeg
from PIL import Image, ImageChops

from specto.align import build_moments
from specto.diff import annotate_recording  # change regions between consecutive stills
from specto.model import Keyframe, Recording, TranscriptSegment
from specto.transcript import parse_transcript, transcribe

DUPLICATE_DISTANCE = 6  # Hamming distance on a 64-bit hash at or below this means "same picture"

Crop = tuple[int, int, int, int]  # (x, y, w, h) in source pixels

# detect_share_region: a pixel "changes" when its grey level differs from the first
# sample by more than this (0-255); a row or column counts when at least this share
# of its pixels change; the box must cover between these shares of the frame.
CHANGE_THRESHOLD = 24
STRIP_FRACTION = 0.02
SHARE_MIN = 0.25
SHARE_MAX = 0.90
# A side is only trimmed when the never-changing margin on it is at least this
# share of that dimension (width for left and right, height for top and bottom).
# A phone app recorded full frame has thin static margins of its own, which are
# part of the app and not a meeting border.
TRIM_MIN_MARGIN = 0.05

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


def _picture_filters(crop: Optional[Crop], max_width: Optional[int]) -> list[str]:
    """The crop and scale steps of an ffmpeg filter chain, crop first so the scale sees the cropped size."""
    steps: list[str] = []
    if crop is not None:
        x, y, w, h = crop
        steps.append(f"crop={w}:{h}:{x}:{y}")
    if max_width is not None:
        steps.append(f"scale=w='min(iw,{max_width})':h=-2")
    return steps


def _grab_frames(
    video_path: str, frames_dir: Path, select_expr: str, max_width: Optional[int], crop: Optional[Crop] = None
) -> list[tuple[Path, float]]:
    """Run ffmpeg once with a `select` filter and return (file, timestamp) pairs.

    `showinfo` sits right after `select`, so it prints one line per kept frame
    and the n-th line belongs to the n-th file ffmpeg writes. Cropping and
    scaling come after both so the timestamps are the source frame's own.
    `max_width=None` keeps the source size.
    """
    _clear(frames_dir, "raw_*.jpg")
    filters = ",".join([f"select='{select_expr}'", "showinfo", *_picture_filters(crop, max_width)])
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


def _sample_frames(
    video_path: str, frames_dir: Path, sample_fps: float, max_width: int, crop: Optional[Crop] = None
) -> list[tuple[Path, float]]:
    """Write one frame every 1/sample_fps seconds and return (file, timestamp) pairs.

    ffmpeg's `fps` filter puts its output on an exact grid, so the n-th file
    (counted from 0) is the frame at n / sample_fps seconds; no log parsing
    needed. A one-hour recording at 1 fps is 3,600 small JPEGs on disk, which
    is fine; only their paths are held in memory.
    """
    _clear(frames_dir, "sample_*.jpg")
    filters = ",".join([f"fps={sample_fps:g}", *_picture_filters(crop, max_width)])
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
    video_path: str, frames_dir: Path, scene_threshold: float, min_gap: float, max_width: int, crop: Optional[Crop] = None
) -> tuple[list[tuple[Path, float]], int]:
    """ffmpeg scene detection (brightness change), then near-duplicate and gap removal.

    Returns the kept (file, timestamp) pairs and how many frames the detector
    found before thinning, which decides whether the screen counts as static.
    Rejected files are deleted as we go. The scene score is taken on the
    cropped picture, so a participant tile outside the crop cannot trigger it.
    """
    scene_expr = f"gt(scene,{scene_threshold})+eq(n,0)"
    grabbed = _grab_frames(video_path, frames_dir, scene_expr, max_width, crop)
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
    video_path: str,
    frames_dir: Path,
    sample_fps: float,
    hash_distance: int,
    min_gap: float,
    max_width: int,
    crop: Optional[Crop] = None,
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
    samples = _sample_frames(video_path, frames_dir, sample_fps, max_width, crop)
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


def detect_share_region(
    video_path: str | Path, samples: int = 12, log: Callable[[str], None] = print
) -> Optional[Crop]:
    """Find the part of the picture that changes over the recording: the shared window.

    A meeting recording of a screen share has the shared window in the middle
    and, around it, things that never change: a dark border, a toolbar, the
    gallery strip. This takes `samples` frames spread evenly over the video
    and marks every pixel whose grey level ever differs from the first
    sample's by more than CHANGE_THRESHOLD. Rows and columns where fewer than
    STRIP_FRACTION of the pixels are marked do not count (a cursor, a blinking
    icon, a small participant tile), and the box is the span of the rows and
    columns that do, pushed out to even numbers so the video encoder is happy.

    Each side is then trimmed only when the margin on it is at least
    TRIM_MIN_MARGIN of that dimension; a thinner margin is left in, so a
    full-frame app recording keeps its own edges. `log` says which sides were
    trimmed and by how much.

    Returns (x, y, w, h) in source pixels, or None when there is nothing
    worth cropping: the changing area covers more than SHARE_MAX of the frame
    (no border to remove) or less than SHARE_MIN of it (too small to be a
    shared window, so probably wrong), or every margin is too thin to trim.
    All of these are explained through `log`.
    """
    video_path = str(video_path)
    duration = probe_duration(video_path)
    interval = max(duration / samples, 0.05)
    select_expr = f"isnan(prev_selected_t)+gte(t-prev_selected_t,{interval:.3f})"
    with tempfile.TemporaryDirectory(prefix="specto_share_") as tmp:
        grabbed = _grab_frames(video_path, Path(tmp), select_expr, max_width=None)
        if len(grabbed) < 2:
            log("ingest: too few frames to tell which part of the picture changes, keeping the whole frame")
            return None
        with Image.open(grabbed[0][0]) as img:
            first = img.convert("L")
        changed = Image.new("L", first.size, 0)  # per pixel: the most it ever differed from the first sample
        for path, _ in grabbed[1:]:
            with Image.open(path) as img:
                changed = ImageChops.lighter(changed, ImageChops.difference(first, img.convert("L")))

    width, height = changed.size
    mask = changed.point(lambda v: 255 if v > CHANGE_THRESHOLD else 0)
    # A BOX resize down to one column (or one row) averages each row (or column):
    # the value is 255 times the share of pixels in it that changed.
    row_shares = mask.resize((1, height), Image.Resampling.BOX).tobytes()
    col_shares = mask.resize((width, 1), Image.Resampling.BOX).tobytes()
    cutoff = STRIP_FRACTION * 255
    busy_rows = [i for i, share in enumerate(row_shares) if share >= cutoff]
    busy_cols = [i for i, share in enumerate(col_shares) if share >= cutoff]
    if not busy_rows or not busy_cols:
        log("ingest: nothing in the picture changes over the recording, keeping the whole frame")
        return None
    x0, x1 = busy_cols[0], busy_cols[-1] + 1
    y0, y1 = busy_rows[0], busy_rows[-1] + 1
    x0, y0 = x0 - x0 % 2, y0 - y0 % 2
    x1, y1 = min(width, x1 + x1 % 2), min(height, y1 + y1 % 2)
    share = ((x1 - x0) * (y1 - y0)) / (width * height)
    if share > SHARE_MAX:
        log(f"ingest: the changing area is {share:.0%} of the {width}x{height} frame, so there is no border to crop")
        return None
    if share < SHARE_MIN:
        log(
            f"ingest: the changing area is only {share:.0%} of the {width}x{height} frame, "
            f"too small for a shared window, so the whole frame is kept"
        )
        return None
    margins = {"left": x0, "top": y0, "right": width - x1, "bottom": height - y1}
    trimmed = {
        side: px
        for side, px in margins.items()
        if px >= TRIM_MIN_MARGIN * (width if side in ("left", "right") else height)
    }
    if not trimmed:
        log(
            f"ingest: the margins around the changing area are all under {TRIM_MIN_MARGIN:.0%} of the "
            f"{width}x{height} frame (widest {max(margins.values())} px), so there is no border to crop"
        )
        return None
    kept = [side for side in margins if side not in trimmed]
    message = "ingest: trimming " + ", ".join(f"{side} {px} px" for side, px in trimmed.items())
    if kept:
        message += f"; kept {', '.join(kept)} (margin under {TRIM_MIN_MARGIN:.0%})"
    log(message)
    x0 = x0 if "left" in trimmed else 0
    y0 = y0 if "top" in trimmed else 0
    x1 = x1 if "right" in trimmed else width
    y1 = y1 if "bottom" in trimmed else height
    return (x0, y0, x1 - x0, y1 - y0)


def _thin_by_distance(kept: list[tuple[Path, float]], max_frames: int) -> tuple[list[tuple[Path, float]], int]:
    """Drop the frames that differ least from the frame before them until `max_frames` are left.

    Every frame gets a `content_hash` and a score: the distance from the
    previous surviving frame. The lowest score goes first and the frame after
    it is re-scored against its new neighbour, so three frames that each
    drift a little from the last are not all lost in one go. Frame 0 is never
    dropped. Dropped files are deleted. Returns the survivors, in order, and
    the largest score among the dropped frames, so the log can say how much
    was thrown away.
    """
    n = len(kept)
    hashes = [content_hash(path) for path, _ in kept]
    prev = list(range(-1, n - 1))
    nxt = list(range(1, n + 1))
    alive = [True] * n
    distance = [0] + [hamming(hashes[i], hashes[i - 1]) for i in range(1, n)]
    heap = [(distance[i], i) for i in range(1, n)]
    heapq.heapify(heap)
    remaining, largest = n, 0
    while remaining > max_frames and heap:
        score, i = heapq.heappop(heap)
        if not alive[i] or score != distance[i]:  # already dropped, or re-scored since it was queued
            continue
        alive[i] = False
        remaining -= 1
        largest = max(largest, score)
        kept[i][0].unlink()
        before, after = prev[i], nxt[i]
        nxt[before] = after
        if after < n:
            prev[after] = before
            distance[after] = hamming(hashes[after], hashes[before])
            heapq.heappush(heap, (distance[after], after))
    return [kept[i] for i in range(n) if alive[i]], largest


def _resolve_crop(video_path: str, crop: Optional[Crop | str]) -> Optional[Crop]:
    """Turn the `crop` argument of `extract_keyframes` into a box, or None for the whole frame."""
    if crop is None:
        return None
    if isinstance(crop, str):
        if crop != "auto":
            raise ValueError(f"crop must be (x, y, w, h) or 'auto', not {crop!r}")
        box = detect_share_region(video_path)
        if box is not None:
            x, y, w, h = box
            print(f"ingest: cropping to the {w}x{h} area at {x},{y}, the rest of the frame never changes")
        return box
    try:
        x, y, w, h = (int(v) for v in crop)
    except (TypeError, ValueError):
        raise ValueError(f"crop must be (x, y, w, h) or 'auto', not {crop!r}") from None
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise ValueError(f"crop must have x, y >= 0 and w, h > 0, not {crop!r}")
    print(f"ingest: cropping to the {w}x{h} area at {x},{y}")
    return (x, y, w, h)


def extract_keyframes(
    video_path: str | Path,
    out_dir: str | Path,
    detect: str = "hash",
    sample_fps: float = 1.0,
    hash_distance: int = 8,
    scene_threshold: float = 0.3,
    min_gap: float = 1.5,
    max_frames: int = 240,
    max_width: int = 1280,
    fallback_interval: float = 10.0,
    crop: Optional[Crop | str] = None,
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

    `crop` cuts the picture down before anything looks at it: (x, y, w, h) in
    source pixels, or "auto" to keep only the part that changes over the
    recording (see `detect_share_region`), which on a meeting recording is
    the shared window without the border, toolbar and gallery strip around
    it. Frame sizes are the cropped size, scaled down to `max_width` if wider.

    Both detectors then drop frames closer than `min_gap` seconds to the
    previous kept one (frame 0 is always kept). If more than `max_frames` are
    left, the ones that differ least from their neighbour go first, so a long
    session loses its cursor moves and tooltips before it loses a screen; the
    log says how many went and how different the most different of them was.
    A recording of a screen that never changes gives fewer than three frames
    in either mode, so then we sample one frame every `fallback_interval`
    seconds instead (no duplicate removal, because those frames are meant to
    look alike). Frames end up as frames/frame_NNNN.jpg, numbered
    contiguously from 0 in time order; everything else is deleted.
    """
    video_path = str(video_path)
    out_dir = Path(out_dir)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for pattern in ("raw_*.jpg", "sample_*.jpg"):  # leftovers from a run that crashed part-way
        _clear(frames_dir, pattern)
    if detect not in ("hash", "scene"):
        raise ValueError(f"detect must be 'hash' or 'scene', not {detect!r}")
    box = _resolve_crop(video_path, crop)

    if detect == "hash":
        kept, detected = _detect_hash(video_path, frames_dir, sample_fps, hash_distance, min_gap, max_width, box)
    else:
        kept, detected = _detect_scene(video_path, frames_dir, scene_threshold, min_gap, max_width, box)

    if detected < 3:
        for path, _ in kept:
            path.unlink()
        sample_expr = f"isnan(prev_selected_t)+gte(t-prev_selected_t,{fallback_interval})"
        kept = _grab_frames(video_path, frames_dir, sample_expr, max_width, box)
        print(f"ingest: static screen, sampled {len(kept)} frames every {fallback_interval:g}s instead")

    if len(kept) > max_frames:
        before = len(kept)
        kept, largest = _thin_by_distance(kept, max_frames)
        print(
            f"ingest: dropped {before - len(kept)} frames that differed from their neighbour by {largest} or less "
            f"to stay within {max_frames} frames; raise max_frames if that is too much"
        )

    for pattern in ("frame_*.jpg", "crop_*.jpg"):  # close-ups from an earlier run must not survive
        _clear(frames_dir, pattern)
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
    `max_width`, `fallback_interval`, `crop`). If the
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
    # --- specto.diff: where each still differs from the previous one, plus close-ups
    recording = annotate_recording(recording, out_dir)
    # ---
    recording_json.write_text(recording.model_dump_json(indent=2), encoding="utf-8")
    print(f"ingest: wrote {recording_json}")
    return recording


# ---------------------------------------------------------- screenshot folders
#
# Sometimes there is no recording: the expert sent a folder of screenshots and
# a written walkthrough, or the analyst took screenshots during the call. The
# same recording.json is built from those, with the frames fed through the
# live store so duplicates are dropped and change boxes are computed the same
# way as for a video.

FOLDER_STEP = 10.0  # seconds between screenshots whose names carry no time, and after the last one
NOTES_SUFFIXES = (".txt", ".md")

# A name that is a number of seconds: `12`, `shot_12`, `shot_0012.5`, `screenshot-12`, `t12`.
# `IMG_0001` is a counter, not a time, so only these prefixes (or none) count.
_SECONDS_NAME_RE = re.compile(r"^(?:shot|screenshot|t|time|sec|secs)?[ _-]?\d+(?:\.\d+)?$", re.I)
# A date and time in the name: `2026-09-12T10-04-33`, `Screenshot 2026-09-12 at 10.04.33 AM`,
# `Screenshot 2026-09-12 100433` (Windows). Seconds are counted from the earliest such name.
_ISO_NAME_RE = re.compile(
    r"(?<!\d)(\d{4})-(\d{2})-(\d{2})[T _]+(?:at[ _]+)?(\d{2})[-:.]?(\d{2})[-:.]?(\d{2})(?:[.,](\d{1,6}))?(?:[ _]?([AaPp][Mm]))?"
)
_NOTE_MARKER_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")


def _natural_key(name: str) -> list[tuple[int, int | str]]:
    """Sort key that puts `IMG_2` before `IMG_10`: digit runs compare as numbers."""
    return [(0, int(part)) if part.isdigit() else (1, part.lower()) for part in re.split(r"(\d+)", name)]


def _iso_stamp(stem: str) -> Optional[datetime]:
    match = _ISO_NAME_RE.search(stem)
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, meridian = match.groups()
    hour = int(hour)
    if meridian:
        hour = hour % 12 + (12 if meridian.lower() == "pm" else 0)
    micro = int((fraction or "0").ljust(6, "0")[:6])
    try:
        return datetime(int(year), int(month), int(day), hour, int(minute), int(second), micro)
    except ValueError:  # a date-looking run of digits that is not a date
        return None


def screenshot_times(
    paths: list[Path], step: float = FOLDER_STEP, log: Callable[[str], None] = print
) -> list[tuple[Path, float]]:
    """Give every screenshot a time in seconds and return (file, time) pairs in time order.

    A name that carries a time keeps it: a plain number of seconds (`shot_12.png`,
    `shot_0012.5.png`, `12.png`) or a date and time (`2026-09-12T10-04-33.png`,
    counted from the earliest such name). Files whose names carry no time
    (`IMG_0001.png`) follow the last timed one in name order, `step` seconds
    apart; when no name carries a time they start at 0, and the log says so.
    """
    from specto.live import shot_time  # here, not at the top: live imports this module

    ordered = sorted(paths, key=lambda p: _natural_key(p.name))
    timed: dict[Path, float] = {}
    stamped: dict[Path, datetime] = {}
    for path in ordered:
        if _SECONDS_NAME_RE.match(path.stem):
            timed[path] = shot_time(path)
            continue
        stamp = _iso_stamp(path.stem)
        if stamp is not None:
            stamped[path] = stamp
    if stamped:
        first = min(stamped.values())
        for path, stamp in stamped.items():
            timed[path] = (stamp - first).total_seconds()
    untimed = [p for p in ordered if p not in timed]
    if not timed:
        log(f"ingest: no screenshot name carries a time, so they are spaced {step:g}s apart in name order")
        return [(path, i * step) for i, path in enumerate(untimed)]
    result = sorted(timed.items(), key=lambda item: (item[1], _natural_key(item[0].name)))
    if untimed:
        last = result[-1][1]
        log(
            f"ingest: {len(timed)} screenshot names carry a time; the other {len(untimed)} "
            f"follow in name order at {step:g}s steps after {last:g}s"
        )
        result += [(path, last + (i + 1) * step) for i, path in enumerate(untimed)]
    return result


def split_notes(text: str) -> list[str]:
    """Break a notes file with no timestamps into the pieces that go with one screen each.

    A paragraph (lines between blank lines) is one note. A paragraph whose lines
    all start with a bullet or a number (`- `, `* `, `1. `, `2) `) is one note
    per line, marker removed. A heading on its own is not a note; it is put in
    front of the next note as `Heading: text`.
    """
    notes: list[str] = []
    heading: Optional[str] = None
    for paragraph in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [line.strip() for line in paragraph.split("\n") if line.strip()]
        if not lines:
            continue
        if all(_HEADING_RE.match(line) for line in lines):
            heading = " ".join(_HEADING_RE.sub("", line) for line in lines)
            continue
        if all(_NOTE_MARKER_RE.match(line) for line in lines):
            pieces = [_NOTE_MARKER_RE.sub("", line) for line in lines]
        else:
            pieces = [" ".join(_HEADING_RE.sub("", line) for line in lines)]
        if heading:
            pieces[0] = f"{heading}: {pieces[0]}"
            heading = None
        notes.extend(pieces)
    return notes


def spread_notes(notes: list[str], keyframes: list[Keyframe], duration: float) -> list[TranscriptSegment]:
    """Turn untimed notes into segments laid over the frames in order.

    Note i goes with frame i when there are as many notes as frames; otherwise
    frame `i * frames // notes`, so both lists are walked at the same pace.
    Each segment spans its frame's interval (up to the next frame, or
    `duration` for the last); several notes on one frame share that interval
    in equal parts so they stay in order.
    """
    if not notes or not keyframes:
        return []
    ordered = sorted(keyframes, key=lambda k: k.timestamp)
    ends = [k.timestamp for k in ordered[1:]] + [max(duration, ordered[-1].timestamp)]
    groups: list[list[str]] = [[] for _ in ordered]
    for i, note in enumerate(notes):
        groups[i * len(ordered) // len(notes)].append(note)
    segments: list[TranscriptSegment] = []
    for keyframe, end, group in zip(ordered, ends, groups):
        span = (end - keyframe.timestamp) / len(group) if group else 0.0
        for j, note in enumerate(group):
            start = keyframe.timestamp + j * span
            segments.append(TranscriptSegment(start=round(start, 3), end=round(start + span, 3), text=note))
    return segments


def ingest_folder(
    folder: str | Path,
    out_dir: str | Path,
    transcript_path: Optional[str | Path] = None,
    force: bool = False,
    hash_distance: int = 8,
    min_gap: float = 0.0,
    log: Callable[[str], None] = print,
) -> Recording:
    """Build `out_dir/recording.json` from a folder of screenshots instead of a video.

    The screenshots are the png, jpg and webp files in `folder`, timed from
    their names (see `screenshot_times`). They go through the live store's
    `add_screenshot`, so a screenshot that repeats the one before it is
    dropped, the kept ones are scaled and saved as `frames/frame_NNNN.jpg`,
    and each gets its change box and close-up. `min_gap` is 0 here because
    every screenshot was taken on purpose.

    `transcript_path` is read like any transcript (.vtt, .srt, .json, timed
    .txt). A .txt or .md with no timestamps is taken as written notes: its
    paragraphs are spread over the frames in order (see `split_notes` and
    `spread_notes`). With no transcript there are no segments; screenshots
    have no audio to transcribe. The duration is the last frame's time plus
    FOLDER_STEP seconds and the source is the folder's name. An existing
    recording.json is loaded and returned unless `force` is set.
    """
    from specto.live import SCREENSHOT_SUFFIXES, LiveSession  # here, not at the top: live imports this module

    folder = Path(folder)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    recording_json = out_dir / "recording.json"
    if recording_json.exists() and not force:
        log(f"ingest: {recording_json} already exists, loading it (use force=True to redo)")
        return Recording.model_validate_json(recording_json.read_text(encoding="utf-8"))

    shots = [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in SCREENSHOT_SUFFIXES and not p.name.startswith(".")
    ]
    if not shots:
        raise FileNotFoundError(f"no screenshots (png, jpg, webp) in {folder}")

    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for pattern in ("frame_*.jpg", "crop_*.jpg", "pending.jpg"):  # a previous run, or one that crashed part-way
        _clear(frames_dir, pattern)
    source = folder.name or folder.resolve().name
    session = LiveSession(out_dir, hash_distance=hash_distance, min_gap=min_gap, source=source)
    dropped = 0
    for path, timestamp in screenshot_times(shots, log=log):
        if session.add_screenshot(path, timestamp) is None:
            dropped += 1
    keyframes = session.recording.keyframes
    log(
        f"ingest: {len(shots)} screenshots in {folder}, {len(keyframes)} kept as frames, "
        f"{dropped} dropped as repeats of the frame before"
    )
    duration = keyframes[-1].timestamp + FOLDER_STEP

    segments: list[TranscriptSegment] = []
    transcript_source = "none"
    if transcript_path is not None:
        transcript_path = Path(transcript_path)
        transcript_source = "file"
        segments = parse_transcript(transcript_path)
        if segments:
            log(f"ingest: {transcript_path.name} carries its own times, so its {len(segments)} segments are placed by time")
        elif transcript_path.suffix.lower() in NOTES_SUFFIXES:
            notes = split_notes(transcript_path.read_text(encoding="utf-8-sig"))
            segments = spread_notes(notes, keyframes, duration)
            log(
                f"ingest: {transcript_path.name} has no timestamps, so its {len(notes)} notes "
                f"are spread over the {len(keyframes)} frames in order"
            )
        else:
            log(f"ingest: {transcript_path.name} holds no segments")
    else:
        log("ingest: no transcript given, frames only (screenshots have no audio to transcribe)")

    moments = build_moments(keyframes, segments, duration)
    recording = Recording(
        source=source,
        duration=duration,
        keyframes=keyframes,
        segments=segments,
        moments=moments,
        transcript_source=transcript_source,
    )
    recording_json.write_text(recording.model_dump_json(indent=2), encoding="utf-8")
    log(f"ingest: wrote {recording_json}")
    return recording
