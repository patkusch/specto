"""Shared fixtures: synthetic videos built with Pillow and the bundled ffmpeg.

No real recording lives in the repo. Each test session draws a handful of fake
"screens", encodes them with ffmpeg, and points the pipeline at that file.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg
import pytest
from PIL import Image, ImageDraw, ImageFont

FPS = 2
SECONDS_PER_SCREEN = 3
# Backgrounds alternate dark and light on purpose: ffmpeg's scene score looks at
# brightness only, so two screens of the same brightness in different hues
# (say mid-green then mid-red) count as no change at all.
SCREENS = [
    ("Customer Search", (40, 70, 140), 1),
    ("Customer Details", (245, 245, 245), 5),
    ("Approval Queue", (30, 120, 60), 3),
    ("Done", (250, 235, 215), 0),
]


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow: no size argument
        return ImageFont.load_default()


def draw_screen(title: str, background: tuple[int, int, int], rows: int, size=(640, 360)) -> Image.Image:
    """A fake application screen: header bar, big title, and some field rows."""
    img = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(img)
    light = sum(background) > 380
    fg = (20, 20, 20) if light else (255, 255, 255)
    draw.rectangle([0, 0, size[0], 40], fill=(0, 0, 0) if light else (255, 255, 255))
    draw.text((16, 8), "ACME Portal", fill=(255, 255, 255) if light else (0, 0, 0), font=_font(22))
    draw.text((30, 70), title, fill=fg, font=_font(44))
    for i in range(rows):
        y = 150 + i * 36
        draw.rectangle([30, y, 160, y + 24], outline=fg)
        draw.rectangle([180, y, 600, y + 24], fill=(255, 255, 255) if not light else (220, 220, 220), outline=fg)
    return img


def encode_frames(frame_dir: Path, out_file: Path, fps: int = FPS) -> Path:
    """Encode `frame_dir/img_%04d.png` into an mp4; libx264 if present, else mpeg4."""
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    encoders = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    codec = "libx264" if "libx264" in encoders else "mpeg4"
    cmd = [
        exe, "-hide_banner", "-loglevel", "error", "-y",
        "-framerate", str(fps), "-i", str(frame_dir / "img_%04d.png"),
        "-c:v", codec, "-pix_fmt", "yuv420p", "-r", str(fps),
        str(out_file),
    ]
    subprocess.run(cmd, check=True)
    return out_file


@pytest.fixture(scope="session")
def synthetic_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A ~12 second mp4: four different screens, three seconds each."""
    work = tmp_path_factory.mktemp("synthetic")
    frame_dir = work / "png"
    frame_dir.mkdir()
    n = 0
    for title, background, rows in SCREENS:
        img = draw_screen(title, background, rows)
        for _ in range(FPS * SECONDS_PER_SCREEN):
            img.save(frame_dir / f"img_{n:04d}.png")
            n += 1
    return encode_frames(frame_dir, work / "walkthrough.mp4")


@pytest.fixture(scope="session")
def static_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 25 second mp4 of one unchanging screen, for the sampling fallback."""
    work = tmp_path_factory.mktemp("static")
    frame_dir = work / "png"
    frame_dir.mkdir()
    img = draw_screen("Nothing Happens", (90, 90, 120), 2)
    for n in range(FPS * 25):
        img.save(frame_dir / f"img_{n:04d}.png")
    return encode_frames(frame_dir, work / "static.mp4")
