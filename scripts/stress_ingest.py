"""Stress the ingest stage with a synthetic one-hour screen share.

Builds a fake recording (distinct app pages shown for a while each, values typed
into forms over time, a cursor that keeps moving, the odd popup) and then runs
the stages one by one with timings and peak memory:

    extract_keyframes  ->  annotate_recording  ->  ocr_recording  ->  estimate

Usage:
    .venv/bin/python scripts/stress_ingest.py [--minutes 60] [--screens 40] [--fps 2]
                                              [--size 640x360] [--out DIR] [--seed 7]
                                              [--reuse] [--profile] [--no-ocr]

Importable without running anything: the smoke test uses `build_video`.
"""
from __future__ import annotations

import argparse
import colorsys
import contextlib
import cProfile
import io
import platform
import pstats
import random
import re
import resource
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

import imageio_ffmpeg
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.conftest import _font, draw_screen  # noqa: E402  (the fixtures' drawing style)

BASE_SIZE = (640, 360)  # draw_screen's native size; bigger sizes are scaled up from it
MIN_DWELL = 20.0
MAX_DWELL = 180.0
CHAR_SECONDS = 0.4  # one typed character every this many seconds
POPUP_CHANCE = 0.5  # share of page visits that get a popup
POPUP_SECONDS = (2.0, 6.0)
CURSOR_RADIUS = 3  # 6 px across at 640 wide, about 1% of the frame, like a real pointer
CURSOR_SPEED = 14  # pixels per frame at BASE_SIZE

TITLES = [
    "Customer Search", "Customer Details", "Approval Queue", "Reports", "Settings",
    "Audit Log", "Invoices", "Dashboard", "Users", "Help", "Orders", "Order Details",
    "Shipments", "Returns", "Refunds", "Products", "Product Details", "Inventory",
    "Suppliers", "Purchase Orders", "Payments", "Payment Details", "Disputes",
    "Notifications", "Templates", "Workflows", "Tasks", "Calendar", "Contacts",
    "Accounts", "Account Details", "Contracts", "Renewals", "Pricing", "Discounts",
    "Tax Rules", "Regions", "Warehouses", "Integrations", "Import Data", "Export Data",
    "Activity", "Approvals", "Roles", "Permissions", "API Keys", "Webhooks", "Logout",
]
VALUES = [
    "Jane Smith", "SW1A 1AA", "07000 12345678", "INV-2024-0093", "jane@example.com",
    "ACME Ltd", "Pending review", "12/09/2026", "GBP 1,250.00", "Priority: high",
    "ORD-77812", "Warehouse B", "Net 30", "Approved by K. Lee", "Ref 44-AB-9",
]
POPUP_TEXTS = ["Saved", "Loading...", "3 results", "Copied", "Session refreshed"]


# ------------------------------------------------------------------ the plan


@dataclass
class Popup:
    start: float
    end: float
    x: int
    y: int
    text: str


@dataclass
class Typing:
    row: int
    start: float
    text: str


@dataclass
class Visit:
    """One stretch of the recording spent on one page."""

    screen: int
    start: float
    seconds: float
    typing: list[Typing] = field(default_factory=list)
    popups: list[Popup] = field(default_factory=list)

    @property
    def end(self) -> float:
        return self.start + self.seconds


@dataclass
class Screen:
    title: str
    background: tuple[int, int, int]
    rows: int
    form: bool  # values get typed into its rows over time


def make_screens(count: int, rng: random.Random) -> list[Screen]:
    """`count` distinct pages: titles, hues and row counts all differ; backgrounds alternate dark and light."""
    screens: list[Screen] = []
    for i in range(count):
        title = TITLES[i % len(TITLES)] + ("" if i < len(TITLES) else f" {i // len(TITLES) + 1}")
        light = i % 2 == 1
        hue = (i * 0.618034 + rng.random() * 0.05) % 1.0  # golden-ratio spacing keeps neighbours apart
        if light:
            r, g, b = colorsys.hsv_to_rgb(hue, 0.08 + rng.random() * 0.08, 0.93 + rng.random() * 0.05)
        else:
            r, g, b = colorsys.hsv_to_rgb(hue, 0.55 + rng.random() * 0.3, 0.35 + rng.random() * 0.2)
        rows = rng.randint(0, 5)
        form = rows > 0 and i % 3 == 1
        screens.append(Screen(title, (int(r * 255), int(g * 255), int(b * 255)), rows, form))
    return screens


def plan_visits(
    total_seconds: float,
    screens: list[Screen],
    rng: random.Random,
    popup_chance: float = POPUP_CHANCE,
    typing: bool = True,
) -> list[Visit]:
    """Every screen once, in a shuffled order, each for a random 20 to 180 s; the dwells are then
    scaled so they add up to `total_seconds` exactly (so a short run still shows every page).
    `typing=False` leaves the form pages empty (the smoke test wants one keyframe per page)."""
    order = list(range(len(screens)))
    rng.shuffle(order)
    dwells = [rng.uniform(MIN_DWELL, MAX_DWELL) for _ in order]
    scale = total_seconds / sum(dwells)
    dwells = [d * scale for d in dwells]
    visits: list[Visit] = []
    t = 0.0
    for screen_index, dwell in zip(order, dwells):
        visit = Visit(screen=screen_index, start=t, seconds=dwell)
        screen = screens[screen_index]
        if screen.form and typing:
            values = rng.sample(VALUES, min(3, screen.rows))
            offset = rng.uniform(0.15, 0.4) * dwell
            for row, text in enumerate(values):
                visit.typing.append(Typing(row=row, start=t + offset, text=text))
                offset += len(text) * CHAR_SECONDS + rng.uniform(1.0, 4.0)
        if rng.random() < popup_chance and dwell > 8:
            length = rng.uniform(*POPUP_SECONDS)
            start = t + rng.uniform(2.0, max(2.0, dwell - length - 1))
            visit.popups.append(
                Popup(start, start + length, rng.randint(60, 440), rng.randint(200, 300), rng.choice(POPUP_TEXTS))
            )
        visits.append(visit)
        t += dwell
    return visits


# ---------------------------------------------------------------- the frames


def _page_image(screen: Screen, size: tuple[int, int]) -> Image.Image:
    img = draw_screen(screen.title, screen.background, screen.rows)
    if size != BASE_SIZE:
        img = img.resize(size, Image.Resampling.LANCZOS)
    return img


def iter_frames(
    visits: list[Visit], screens: list[Screen], fps: int, size: tuple[int, int], rng: random.Random
) -> Iterator[bytes]:
    """RGB bytes of every frame in order: page, typed text so far, popup if open, cursor dot."""
    scale = size[0] / BASE_SIZE[0]
    pages = {i: _page_image(s, size) for i, s in enumerate(screens)}
    text_font = _font(max(8, round(18 * scale)))
    popup_font = _font(max(8, round(14 * scale)))
    radius = max(2, round(CURSOR_RADIUS * scale))
    cursor = [size[0] / 2, size[1] / 2]
    target = [rng.uniform(0, size[0]), rng.uniform(0, size[1])]
    speed = CURSOR_SPEED * scale
    total_frames = round(visits[-1].end * fps)
    visit_iter = iter(visits)
    visit = next(visit_iter)
    for n in range(total_frames):
        t = n / fps
        while t >= visit.end and visit is not visits[-1]:
            visit = next(visit_iter)
        frame = pages[visit.screen].copy()
        draw = ImageDraw.Draw(frame)
        light = sum(screens[visit.screen].background) > 380
        fg = (20, 20, 20) if light else (255, 255, 255)
        for typing in visit.typing:
            if t < typing.start:
                continue
            shown = typing.text[: int((t - typing.start) / CHAR_SECONDS)]
            if shown:
                draw.text((186 * scale, (153 + typing.row * 36) * scale), shown, fill=(20, 20, 20), font=text_font)
        for popup in visit.popups:
            if popup.start <= t < popup.end:
                x, y = popup.x * scale, popup.y * scale
                draw.rectangle([x, y, x + 140 * scale, y + 44 * scale], fill=(255, 255, 240), outline=(0, 0, 0))
                draw.text((x + 10 * scale, y + 12 * scale), popup.text, fill=(20, 20, 20), font=popup_font)
        dx, dy = target[0] - cursor[0], target[1] - cursor[1]
        dist = (dx * dx + dy * dy) ** 0.5
        if dist < speed:
            cursor[:] = target
            target[:] = [rng.uniform(0, size[0]), rng.uniform(0, size[1])]
        else:
            cursor[0] += dx / dist * speed
            cursor[1] += dy / dist * speed
        cx, cy = cursor
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=fg, outline=(128, 128, 128))
        yield frame.tobytes()


def _codec(exe: str) -> str:
    encoders = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libx264" if "libx264" in encoders else "mpeg4"


def build_video(
    out_file: Path | str,
    minutes: float = 60,
    screens: int = 40,
    fps: int = 2,
    size: tuple[int, int] = BASE_SIZE,
    seed: int = 7,
    popup_chance: float = POPUP_CHANCE,
    typing: bool = True,
    log: Callable[[str], None] = print,
) -> tuple[Path, list[Visit], list[Screen]]:
    """Build the synthetic recording at `out_file`; returns the path, the visits and the screens.

    Frames are piped straight into ffmpeg as raw RGB, so nothing is written to
    disk but the mp4.
    """
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    screen_list = make_screens(screens, rng)
    visits = plan_visits(minutes * 60, screen_list, rng, popup_chance, typing)
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    codec = _codec(exe)
    quality = ["-preset", "veryfast", "-crf", "23"] if codec == "libx264" else ["-q:v", "5"]
    cmd = [
        exe, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}", "-r", str(fps), "-i", "-",
        "-c:v", codec, *quality, "-pix_fmt", "yuv420p", "-r", str(fps),
        str(out_file),
    ]
    started = time.perf_counter()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    frames = 0
    try:
        for data in iter_frames(visits, screen_list, fps, size, rng):
            proc.stdin.write(data)
            frames += 1
    finally:
        # communicate() closes stdin itself; closing it first makes Python 3.12
        # raise "flush of closed file" when it tries again.
        _, stderr = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed while encoding: {stderr.decode(errors='replace')}")
    elapsed = time.perf_counter() - started
    typed = sum(len(v.typing) for v in visits)
    popups = sum(len(v.popups) for v in visits)
    log(
        f"build: {out_file.name} {size[0]}x{size[1]} {codec}, {minutes:g} min, {frames} frames at {fps} fps, "
        f"{len(screen_list)} screens over {len(visits)} visits ({typed} typed values, {popups} popups): "
        f"{elapsed:.1f}s, {out_file.stat().st_size / 2**20:.1f} MB"
    )
    return out_file, visits, screen_list


# ---------------------------------------------------------------- the stages


def _peak_mb(who: int = resource.RUSAGE_SELF) -> float:
    """Peak resident set of this process (or its finished children) in MiB."""
    maxrss = resource.getrusage(who).ru_maxrss
    return maxrss / 2**20 if platform.system() == "Darwin" else maxrss / 1024


@dataclass
class StageResult:
    name: str
    seconds: float
    peak_mb: float  # this process, after the stage (ru_maxrss never goes down)
    grew_mb: float  # peak after minus peak before
    children_peak_mb: float  # ffmpeg / OCR subprocesses, if any
    profile: str = ""


def run_stage(name: str, fn: Callable[[], object], profile: bool = False) -> tuple[object, StageResult]:
    before = _peak_mb()
    started = time.perf_counter()
    text = ""
    if profile:
        profiler = cProfile.Profile()
        result = profiler.runcall(fn)
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(10)
        text = stream.getvalue()
    else:
        result = fn()
    seconds = time.perf_counter() - started
    after = _peak_mb()
    return result, StageResult(name, seconds, after, after - before, _peak_mb(resource.RUSAGE_CHILDREN), text)


def leftover_files(frames_dir: Path) -> list[Path]:
    """Everything in frames/ that is not a frame_ or crop_ JPEG."""
    if not frames_dir.exists():
        return []
    return sorted(p for p in frames_dir.iterdir() if not p.name.startswith(("frame_", "crop_")))


_SAMPLES_RE = re.compile(r"looked at (\d+) samples")


def run_pipeline(
    video: Path, out_dir: Path, model: str, profile: bool, use_ocr: bool, log: Callable[[str], None] = print
) -> tuple[list[StageResult], dict]:
    """extract -> annotate -> ocr -> estimate, each timed; returns the rows and the facts for the summary."""
    from specto.align import build_moments
    from specto.diff import annotate_recording
    from specto.estimate import estimate, format_estimate
    from specto.ingest import extract_keyframes, probe_duration
    from specto.model import Recording
    from specto.ocr import ocr_available, ocr_recording

    rows: list[StageResult] = []
    facts: dict = {}
    duration = probe_duration(video)
    facts["duration"] = duration

    captured = io.StringIO()

    def extract():
        with contextlib.redirect_stdout(captured):
            return extract_keyframes(video, out_dir, detect="hash")

    keyframes, row = run_stage("extract_keyframes (hash)", extract, profile)
    rows.append(row)
    log(captured.getvalue().rstrip())
    match = _SAMPLES_RE.search(captured.getvalue())
    facts["samples"] = int(match.group(1)) if match else None
    facts["keyframes"] = len(keyframes)
    facts["leftovers"] = leftover_files(out_dir / "frames")

    recording = Recording(
        source=str(video),
        duration=duration,
        keyframes=keyframes,
        moments=build_moments(keyframes, [], duration),
    )
    recording, row = run_stage("annotate_recording (diff/crops)", lambda: annotate_recording(recording, out_dir, log=log), profile)
    rows.append(row)
    crops = sorted((out_dir / "frames").glob("crop_*.jpg"))
    facts["crops"] = sum(1 for c in crops if int(c.stem.split("_")[1]) < len(keyframes))
    facts["stale_crops"] = len(crops) - facts["crops"]  # from an earlier run with more frames

    ocr_text: dict[int, str] = {}
    if use_ocr and ocr_available():
        ocr_text, row = run_stage("ocr_recording", lambda: ocr_recording(recording, out_dir, force=True, log=log), profile)
        rows.append(row)
    else:
        log("ocr: skipped" + ("" if use_ocr else " (--no-ocr)"))

    est, row = run_stage(f"estimate ({model})", lambda: estimate(recording, out_dir, model=model, ocr_text=ocr_text), profile)
    rows.append(row)
    facts["estimate"] = est
    log(format_estimate(est))
    (out_dir / "recording.json").write_text(recording.model_dump_json(indent=2), encoding="utf-8")
    return rows, facts


def format_table(rows: list[StageResult]) -> str:
    width = max(len(r.name) for r in rows)
    lines = [f"{'stage'.ljust(width)}  {'seconds':>8}  {'peak MB':>8}  {'grew MB':>8}  {'children MB':>11}"]
    for r in rows:
        lines.append(
            f"{r.name.ljust(width)}  {r.seconds:>8.1f}  {r.peak_mb:>8.0f}  {r.grew_mb:>8.0f}  {r.children_peak_mb:>11.0f}"
        )
    lines.append(f"{'total'.ljust(width)}  {sum(r.seconds for r in rows):>8.1f}")
    return "\n".join(lines)


def _parse_size(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)x(\d+)", text)
    if not match:
        raise argparse.ArgumentTypeError(f"size must look like 640x360, not {text!r}")
    return int(match.group(1)), int(match.group(2))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--minutes", type=float, default=60)
    parser.add_argument("--screens", type=int, default=40)
    parser.add_argument("--fps", type=int, default=2)
    parser.add_argument("--size", type=_parse_size, default=BASE_SIZE, help="WxH, default 640x360")
    parser.add_argument("--out", type=Path, default=REPO / "out" / "stress")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument("--reuse", action="store_true", help="keep the mp4 already in --out instead of building it again")
    parser.add_argument("--profile", action="store_true", help="cProfile every stage, top 10 by cumulative time")
    parser.add_argument("--no-ocr", action="store_true")
    args = parser.parse_args(argv)

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    video = out_dir / f"stress_{args.minutes:g}min_{args.size[0]}x{args.size[1]}.mp4"
    visits: list[Visit] = []
    screens: list[Screen] = []
    if args.reuse and video.exists():
        print(f"build: reusing {video} ({video.stat().st_size / 2**20:.1f} MB)")
        rng = random.Random(args.seed)
        screens = make_screens(args.screens, rng)
        visits = plan_visits(args.minutes * 60, screens, rng)
    else:
        video, visits, screens = build_video(video, args.minutes, args.screens, args.fps, args.size, args.seed)

    work = out_dir / f"run_{args.size[0]}x{args.size[1]}"
    rows, facts = run_pipeline(video, work, args.model, args.profile, not args.no_ocr)

    print()
    print(format_table(rows))
    print()
    typed = sum(len(v.typing) for v in visits)
    popups = sum(len(v.popups) for v in visits)
    print(f"video: {facts['duration']:.0f}s, {len(screens)} screens generated over {len(visits)} visits, "
          f"{typed} typed values, {popups} popups")
    print(f"ffmpeg samples: {facts['samples']}")
    print(f"keyframes found: {facts['keyframes']} (screens generated: {len(screens)}); close-ups written: {facts['crops']}")
    if facts["stale_crops"]:
        print(f"stale close-ups left from an earlier run with more frames: {facts['stale_crops']} (not cleared by extract_keyframes)")
    est = facts["estimate"]
    print(f"estimated cost on {est.model}: ${est.total_cost_usd:.2f} ({est.calls} calls, {est.images} images)")
    leftovers = facts["leftovers"]
    print(f"temp files left in frames/ after extraction: {len(leftovers)}" + (f" {[p.name for p in leftovers[:5]]}" if leftovers else ""))
    for r in rows:
        if r.profile:
            print(f"\n--- profile: {r.name} ---\n{r.profile}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
