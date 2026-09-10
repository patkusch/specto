"""Where did the screen change? Compare each still with the one before it.

Tools that record a live browser session know where every click landed. From
a video we get the same by looking at what differs between two consecutive
stills: the box around the changed pixels says where the action was, and a
close-up of that box lets the model read a small label or a typed value that
would be a few blurry pixels in the full frame.

Everything here works on the saved JPEGs with Pillow alone; coordinates are
in pixels of the saved frame.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageChops, ImageFilter

from specto.model import ChangedRegion, Recording

DEFAULT_THRESHOLD = 24  # greyscale difference (0-255) a pixel must exceed to count as changed
IGNORE_FRACTION_BELOW = 0.002  # under 0.2% of the frame is treated as "nothing changed"
WHOLE_SCREEN_FRACTION = 0.6  # over this share of pixels changed, say "the whole screen"
WHOLE_SCREEN_BOX = 0.5  # a box over half the frame is a page change, not a spot: say "the whole screen"
CROP_MAX_BOX = 0.35  # no close-up when the box covers this much of the frame or more
CROP_MAX_WIDTH = 1280


def changed_region(
    prev_path: str | Path,
    curr_path: str | Path,
    threshold: int = DEFAULT_THRESHOLD,
    ignore_fraction_below: float = IGNORE_FRACTION_BELOW,
) -> Optional[ChangedRegion]:
    """The box around the pixels that differ between two stills, or None.

    Both images are turned to greyscale, the previous one resized to the
    current one's size if they differ, and the absolute per-pixel difference
    is taken. A 3x3 minimum filter runs over that difference before the
    threshold, so a changed pixel only counts when its whole 3x3 neighbourhood
    changed too: JPEG ringing and single-pixel speckle vanish, a typed word or
    a pressed button do not. `fraction` is changed pixels over all pixels;
    below `ignore_fraction_below` the answer is None.
    """
    with Image.open(curr_path) as img:
        curr = img.convert("L")
    with Image.open(prev_path) as img:
        prev = img.convert("L")
    if prev.size != curr.size:
        prev = prev.resize(curr.size, Image.Resampling.LANCZOS)
    diff = ImageChops.difference(prev, curr).filter(ImageFilter.MinFilter(3))
    mask = diff.point(lambda value: 255 if value > threshold else 0)
    box = mask.getbbox()
    if box is None:
        return None
    width, height = curr.size
    changed = mask.histogram()[255]
    fraction = changed / (width * height)
    if fraction < ignore_fraction_below:
        return None
    left, top, right, bottom = box
    return ChangedRegion(x=left, y=top, w=right - left, h=bottom - top, fraction=round(fraction, 4))


def _covers_whole_screen(region: ChangedRegion, width: int, height: int) -> bool:
    box_share = (region.w * region.h) / (width * height) if width and height else 1.0
    return region.fraction > WHOLE_SCREEN_FRACTION or box_share > WHOLE_SCREEN_BOX


def describe_region(region: ChangedRegion, width: int, height: int) -> str:
    """A plain phrase for where the box sits: "top left", "centre", "the whole screen"."""
    if _covers_whole_screen(region, width, height):
        return "the whole screen"
    centre_x = region.x + region.w / 2
    centre_y = region.y + region.h / 2
    column = min(2, int(3 * centre_x / width)) if width else 1
    row = min(2, int(3 * centre_y / height)) if height else 1
    rows = ("top", "middle", "lower")
    columns = ("left", "centre", "right")
    if row == 1 and column == 1:
        return "centre"
    return f"{rows[row]} {columns[column]}"


def crop_wanted(region: Optional[ChangedRegion], width: int, height: int) -> bool:
    """A close-up helps only for a change that is real but small."""
    if region is None or region.fraction < IGNORE_FRACTION_BELOW:
        return False
    if not width or not height:
        return False
    return (region.w * region.h) / (width * height) < CROP_MAX_BOX


def crop_region(
    curr_path: str | Path,
    region: ChangedRegion,
    out_path: str | Path,
    pad: int = 24,
    min_width: int = 640,
) -> Optional[Path]:
    """Save a close-up JPEG of `region` (plus `pad` pixels each side) to `out_path`.

    The crop is scaled up so it is at least `min_width` wide, but never past
    CROP_MAX_WIDTH. Returns None, and writes nothing, when the region covers
    CROP_MAX_BOX of the frame or more: a close-up of nearly the whole frame
    adds nothing to the frame itself.
    """
    out_path = Path(out_path)
    with Image.open(curr_path) as img:
        width, height = img.size
        if not crop_wanted(region, width, height):
            return None
        left = max(0, region.x - pad)
        top = max(0, region.y - pad)
        right = min(width, region.x + region.w + pad)
        bottom = min(height, region.y + region.h + pad)
        crop = img.convert("RGB").crop((left, top, right, bottom))
    crop_width, crop_height = crop.size
    scale = max(1.0, min_width / crop_width)
    scale = min(scale, CROP_MAX_WIDTH / crop_width)
    if scale != 1.0:
        new_size = (max(1, round(crop_width * scale)), max(1, round(crop_height * scale)))
        crop = crop.resize(new_size, Image.Resampling.LANCZOS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    crop.save(out_path, "JPEG", quality=90)
    return out_path


def crop_path_for(keyframe_path: str) -> str:
    """frames/frame_0007.jpg -> frames/crop_0007.jpg (relative to the output folder)."""
    path = Path(keyframe_path)
    return str(path.with_name(path.name.replace("frame_", "crop_", 1)).as_posix())


def annotate_recording(
    recording: Recording,
    out_dir: str | Path,
    log: Callable[[str], None] = print,
) -> Recording:
    """Fill `change_from_previous` on every keyframe after the first and write close-ups.

    Runs in place and returns the same Recording. A close-up that already
    exists and is newer than its frame is left alone, so a second call writes
    nothing; a close-up left over for a frame that no longer needs one is
    removed.
    """
    out_dir = Path(out_dir)
    keyframes = recording.keyframes
    changed = 0
    written = 0
    kept = 0
    for previous, keyframe in zip(keyframes, keyframes[1:]):
        prev_path = out_dir / previous.path
        curr_path = out_dir / keyframe.path
        region = changed_region(prev_path, curr_path)
        keyframe.change_from_previous = region
        crop_file = out_dir / crop_path_for(keyframe.path)
        width = keyframe.width or 0
        height = keyframe.height or 0
        if not (width and height):
            with Image.open(curr_path) as img:
                width, height = img.size
        if region is not None:
            changed += 1
        if crop_wanted(region, width, height):
            if crop_file.exists() and crop_file.stat().st_mtime >= curr_path.stat().st_mtime:
                kept += 1
            elif crop_region(curr_path, region, crop_file) is not None:
                written += 1
        elif crop_file.exists():
            crop_file.unlink()
    if keyframes and keyframes[0].change_from_previous is not None:
        keyframes[0].change_from_previous = None
    reused = f", {kept} already there" if kept else ""
    log(
        f"diff: {changed} of {max(0, len(keyframes) - 1)} frames differ from the one before; "
        f"{written} close-ups written{reused}"
    )
    return recording
