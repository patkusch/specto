"""Tests for specto.diff: where a still differs from the previous one. Pillow only."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from specto.diff import annotate_recording, changed_region, crop_region, describe_region
from specto.model import Keyframe, Moment, Recording

SIZE = (640, 360)


def base_screen() -> Image.Image:
    """A light page with a header bar and some text-like boxes, so JPEG has edges to ring on."""
    img = Image.new("RGB", SIZE, (245, 245, 245))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, SIZE[0], 40], fill=(0, 0, 0))
    for i in range(4):
        y = 80 + i * 40
        draw.rectangle([30, y, 160, y + 24], outline=(20, 20, 20))
        draw.rectangle([180, y, 600, y + 24], fill=(255, 255, 255), outline=(20, 20, 20))
        draw.text((36, y + 4), f"Field {i}", fill=(20, 20, 20))
    return img


def save(img: Image.Image, path: Path, quality: int = 85) -> Path:
    img.save(path, "JPEG", quality=quality)
    return path


def test_identical_frames_give_none(tmp_path: Path) -> None:
    a = save(base_screen(), tmp_path / "a.jpg")
    b = save(base_screen(), tmp_path / "b.jpg")
    assert changed_region(a, b) is None


def test_small_box_in_lower_right(tmp_path: Path) -> None:
    before = base_screen()
    after = before.copy()
    box = (520, 300, 580, 320)  # 60 x 20, lower right
    ImageDraw.Draw(after).rectangle(box, fill=(200, 30, 30))
    a = save(before, tmp_path / "a.jpg")
    b = save(after, tmp_path / "b.jpg")

    region = changed_region(a, b)
    assert region is not None
    assert abs(region.x - box[0]) <= 3
    assert abs(region.y - box[1]) <= 3
    assert abs(region.w - 60) <= 4
    assert abs(region.h - 20) <= 4
    assert 0.002 <= region.fraction <= 0.01
    assert describe_region(region, *SIZE) == "lower right"

    crop = crop_region(b, region, tmp_path / "crop.jpg")
    assert crop is not None and crop.exists()
    with Image.open(crop) as img:
        assert 640 <= img.width <= 1280
        assert img.height > 0


def test_whole_frame_colour_change(tmp_path: Path) -> None:
    green = Image.new("RGB", SIZE, (60, 160, 60))
    red = Image.new("RGB", SIZE, (200, 40, 40))
    a = save(green, tmp_path / "a.jpg")
    b = save(red, tmp_path / "b.jpg")

    region = changed_region(a, b)
    assert region is not None
    assert region.fraction > 0.95
    assert (region.x, region.y) == (0, 0)
    assert region.w >= SIZE[0] - 2 and region.h >= SIZE[1] - 2
    assert describe_region(region, *SIZE) == "the whole screen"
    assert crop_region(b, region, tmp_path / "crop.jpg") is None
    assert not (tmp_path / "crop.jpg").exists()


def test_jpeg_noise_alone_is_not_a_change(tmp_path: Path) -> None:
    a = save(base_screen(), tmp_path / "q60.jpg", quality=60)
    b = save(base_screen(), tmp_path / "q80.jpg", quality=80)
    assert changed_region(a, b) is None


def test_previous_frame_is_resized_to_match(tmp_path: Path) -> None:
    """Frames of different sizes still compare (the box is in the current frame's pixels).

    Upscaling the smaller frame blurs every edge, so the box is wider than the
    painted change; the guard is there so an odd-sized frame does not crash
    the run, not for precision.
    """
    small = save(base_screen().resize((320, 180)), tmp_path / "small.jpg")
    after = base_screen()
    ImageDraw.Draw(after).rectangle((40, 300, 140, 340), fill=(0, 0, 0))
    b = save(after, tmp_path / "b.jpg")
    region = changed_region(small, b)
    assert region is not None
    assert region.x <= 40 and region.y <= 300
    assert region.x + region.w >= 140 and region.y + region.h >= 340
    assert region.x + region.w <= SIZE[0] and region.y + region.h <= SIZE[1]


def test_describe_region_positions() -> None:
    from specto.model import ChangedRegion

    def at(x: int, y: int) -> str:
        return describe_region(ChangedRegion(x=x, y=y, w=20, h=20, fraction=0.01), 300, 300)

    assert at(10, 10) == "top left"
    assert at(140, 10) == "top centre"
    assert at(270, 10) == "top right"
    assert at(10, 140) == "middle left"
    assert at(140, 140) == "centre"
    assert at(270, 140) == "middle right"
    assert at(10, 270) == "lower left"
    assert at(140, 270) == "lower centre"
    assert at(270, 270) == "lower right"
    # A page navigation on a white site changes few pixels but a box over half the frame.
    page = ChangedRegion(x=0, y=77, w=1216, h=514, fraction=0.016)
    assert describe_region(page, 1280, 720) == "the whole screen"
    half_page = ChangedRegion(x=0, y=77, w=640, h=514, fraction=0.016)
    assert describe_region(half_page, 1280, 720) == "middle left"


def _three_frame_recording(out_dir: Path) -> Recording:
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True)
    first = base_screen()
    second = first.copy()
    ImageDraw.Draw(second).rectangle((520, 300, 580, 320), fill=(200, 30, 30))  # small change
    third = Image.new("RGB", SIZE, (30, 120, 60))  # whole new page
    keyframes = []
    for i, img in enumerate((first, second, third)):
        rel = f"frames/frame_{i:04d}.jpg"
        save(img, out_dir / rel)
        keyframes.append(Keyframe(index=i, timestamp=5.0 * i, path=rel, width=SIZE[0], height=SIZE[1]))
    moments = [Moment(keyframe_index=i, start=5.0 * i, end=5.0 * (i + 1)) for i in range(3)]
    return Recording(source="x.mp4", duration=15.0, keyframes=keyframes, moments=moments)


def test_annotate_recording_fills_regions_and_is_idempotent(tmp_path: Path) -> None:
    recording = _three_frame_recording(tmp_path)
    lines: list[str] = []

    result = annotate_recording(recording, tmp_path, log=lines.append)

    assert result is recording
    first, second, third = recording.keyframes
    assert first.change_from_previous is None
    assert second.change_from_previous is not None
    assert describe_region(second.change_from_previous, *SIZE) == "lower right"
    assert third.change_from_previous is not None
    assert third.change_from_previous.fraction > 0.9
    assert (tmp_path / "frames" / "crop_0001.jpg").exists()
    assert not (tmp_path / "frames" / "crop_0002.jpg").exists()
    assert len(lines) == 1 and "2 of 2 frames" in lines[0] and "1 close-ups written" in lines[0]

    before = {p.name: p.stat().st_mtime_ns for p in (tmp_path / "frames").iterdir()}
    annotate_recording(recording, tmp_path, log=lines.append)
    after = {p.name: p.stat().st_mtime_ns for p in (tmp_path / "frames").iterdir()}
    assert after == before  # nothing new written, nothing rewritten
    assert "0 close-ups written" in lines[1] and "1 already there" in lines[1]

    # The regions survive a round trip through recording.json.
    reloaded = Recording.model_validate_json(recording.model_dump_json())
    assert reloaded.keyframes[1].change_from_previous == second.change_from_previous
