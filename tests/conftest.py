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


def form_screen(values: list[str]) -> Image.Image:
    """A white form page with three input boxes, holding `values` typed into the first boxes."""
    img = draw_screen("Customer Details", (245, 245, 245), 3)
    draw = ImageDraw.Draw(img)
    for i, value in enumerate(values[:3]):
        draw.text((186, 153 + i * 36), value, fill=(20, 20, 20), font=_font(18))
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


CANVAS = (960, 540)  # the whole meeting-recording frame
SHARE_BOX = (160, 60, 640, 360)  # where the shared window sits in it: (x, y, w, h)


def meeting_frame(screen: Image.Image, dot_on: bool) -> Image.Image:
    """A fake Teams/Zoom recording frame: `screen` in the middle of a dark border,
    a toolbar strip that never changes along the bottom, and a 6x6 dot in the
    top-left corner that blinks (a "recording" light, or a cursor that moves)."""
    frame = Image.new("RGB", CANVAS, (28, 28, 32))
    frame.paste(screen, SHARE_BOX[:2])
    draw = ImageDraw.Draw(frame)
    draw.rectangle([0, 496, CANVAS[0], CANVAS[1]], fill=(60, 60, 66))  # toolbar
    for i in range(6):  # toolbar "buttons"
        x = 300 + i * 60
        draw.ellipse([x, 506, x + 24, 530], fill=(150, 150, 160))
    draw.rectangle([8, 8, 13, 13], fill=(255, 40, 40) if dot_on else (28, 28, 32))
    return frame


@pytest.fixture(scope="session")
def bordered_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The four screens of `synthetic_video` inside a 960x540 meeting frame (see `meeting_frame`)."""
    work = tmp_path_factory.mktemp("bordered")
    frame_dir = work / "png"
    frame_dir.mkdir()
    n = 0
    for title, background, rows in SCREENS:
        screen = draw_screen(title, background, rows)
        for _ in range(FPS * SECONDS_PER_SCREEN):
            meeting_frame(screen, dot_on=n % 2 == 0).save(frame_dir / f"img_{n:04d}.png")
            n += 1
    return encode_frames(frame_dir, work / "meeting.mp4")


def _framed_video(work: Path, canvas: tuple[int, int], box: tuple[int, int, int, int], name: str) -> Path:
    """The four SCREENS pasted at `box` (x, y, w, h) into a static dark `canvas`."""
    frame_dir = work / "png"
    frame_dir.mkdir()
    n = 0
    for title, background, rows in SCREENS:
        framed = Image.new("RGB", canvas, (28, 28, 32))
        framed.paste(draw_screen(title, background, rows, size=box[2:]), box[:2])
        for _ in range(FPS * SECONDS_PER_SCREEN):
            framed.save(frame_dir / f"img_{n:04d}.png")
            n += 1
    return encode_frames(frame_dir, work / name)


PHONE_CANVAS = (360, 780)  # a portrait app recorded full frame ...
PHONE_BOX = (14, 30, 332, 720)  # ... with thin static margins of its own: about 4% on every side


@pytest.fixture(scope="session")
def phone_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The four screens as a full-frame portrait app: static margins of about 4% on each side, no meeting frame."""
    return _framed_video(tmp_path_factory.mktemp("phone"), PHONE_CANVAS, PHONE_BOX, "phone.mp4")


GALLERY_CANVAS = (960, 540)  # wide margins left and right (a gallery on each side), thin ones above and below
GALLERY_BOX = (160, 10, 640, 520)


@pytest.fixture(scope="session")
def gallery_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The four screens with 16.7% margins left and right and under 2% above and below."""
    return _framed_video(tmp_path_factory.mktemp("gallery"), GALLERY_CANVAS, GALLERY_BOX, "gallery.mp4")


# Twelve screens that all differ in colour, title and row count.
MANY_SCREENS = [
    ("Customer Search", (40, 70, 140), 1),
    ("Customer Details", (245, 245, 245), 5),
    ("Approval Queue", (30, 120, 60), 3),
    ("Done", (250, 235, 215), 0),
    ("Reports", (90, 90, 120), 2),
    ("Settings", (160, 60, 60), 4),
    ("Audit Log", (20, 20, 20), 5),
    ("Invoices", (235, 245, 235), 3),
    ("Dashboard", (60, 60, 160), 0),
    ("Users", (250, 250, 230), 4),
    ("Help", (120, 90, 40), 1),
    ("Logout", (230, 230, 250), 2),
]
VARIANT_SECONDS = 2  # each screen, then its variant, this long


def variant_of(screen: Image.Image) -> Image.Image:
    """The same screen with a small dropdown opened in the bottom right corner."""
    variant = screen.copy()
    draw = ImageDraw.Draw(variant)
    draw.rectangle([470, 230, 610, 330], fill=(255, 255, 255), outline=(0, 0, 0))
    for i in range(4):
        draw.rectangle([478, 238 + i * 24, 602, 254 + i * 24], fill=(200, 200, 210))
    return variant


@pytest.fixture(scope="session")
def variants_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 48 second mp4: each of the twelve MANY_SCREENS for 2 s, then its variant for 2 s.

    The distinct screens start at 0, 4, 8, ... 44 s; the variants at 2, 6, ... 46 s.
    """
    work = tmp_path_factory.mktemp("variants")
    frame_dir = work / "png"
    frame_dir.mkdir()
    n = 0
    for title, background, rows in MANY_SCREENS:
        screen = draw_screen(title, background, rows)
        for img in (screen, variant_of(screen)):
            for _ in range(FPS * VARIANT_SECONDS):
                img.save(frame_dir / f"img_{n:04d}.png")
                n += 1
    return encode_frames(frame_dir, work / "variants.mp4")


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


@pytest.fixture(scope="session")
def colour_only_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 9 second mp4: the same layout in mid-green, mid-red, then mid-blue.

    Every page has the same brightness, so ffmpeg's scene score sees no change.
    """
    work = tmp_path_factory.mktemp("colour_only")
    frame_dir = work / "png"
    frame_dir.mkdir()
    n = 0
    for background in ((60, 160, 60), (160, 60, 60), (60, 60, 160)):
        img = draw_screen("Approval Queue", background, 3)
        for _ in range(FPS * SECONDS_PER_SCREEN):
            img.save(frame_dir / f"img_{n:04d}.png")
            n += 1
    return encode_frames(frame_dir, work / "colour_only.mp4")


@pytest.fixture(scope="session")
def typed_form_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 9 second mp4: an empty white form, the same form with three values typed, then the queue.

    Typing into three boxes barely moves the page's brightness, so the scene
    detector misses the second screen.
    """
    work = tmp_path_factory.mktemp("typed_form")
    frame_dir = work / "png"
    frame_dir.mkdir()
    n = 0
    screens = [
        form_screen([]),
        form_screen(["Jane Smith", "SW1A 1AA", "07000 12345678"]),
        draw_screen("Approval Queue", (30, 120, 60), 3),
    ]
    for img in screens:
        for _ in range(FPS * SECONDS_PER_SCREEN):
            img.save(frame_dir / f"img_{n:04d}.png")
            n += 1
    return encode_frames(frame_dir, work / "typed_form.mp4")
