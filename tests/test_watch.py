"""`specto watch`: a shared folder that processes every recording dropped into it."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from specto.cli import main

VTT = """WEBVTT

00:00:00.000 --> 00:00:02.500
<v Sam>This is the customer search screen, we type the postcode here.

00:00:03.200 --> 00:00:05.800
<v Sam>Then we open the customer details and check the account number.

00:00:06.100 --> 00:00:08.700
<v Sam>Save sends it to the approval queue, only a team lead can approve.
"""


def _make_screenshot_folder(shots: Path) -> None:
    from PIL import Image, ImageDraw

    shots.mkdir()
    for n, (colour, label) in enumerate((((40, 70, 140), "Search"), ((245, 245, 245), "Details")), start=1):
        img = Image.new("RGB", (640, 360), colour)
        ImageDraw.Draw(img).text((40, 40), label, fill=(0, 0, 0) if sum(colour) > 380 else (255, 255, 255))
        img.save(shots / f"shot_{n}.png")


def test_watch_once_processes_new_items_and_writes_state(synthetic_video: Path, tmp_path: Path, capsys) -> None:
    folder = tmp_path / "incoming"
    folder.mkdir()
    out = tmp_path / "out"

    shutil.copyfile(synthetic_video, folder / "video_a.mp4")
    (folder / "video_a.vtt").write_text(VTT, encoding="utf-8")
    shutil.copyfile(synthetic_video, folder / "video_b.mp4")
    (folder / "video_b.vtt").write_text(VTT, encoding="utf-8")
    _make_screenshot_folder(folder / "shots_a")
    (folder / "shots_a.md").write_text("We search by postcode.\n\nThen we open the details.\n", encoding="utf-8")

    rc = main(["watch", str(folder), "--out", str(out), "--fake", "--once"])
    assert rc == 0
    text = capsys.readouterr().out
    assert "3 processed, 0 skipped, 0 failed" in text

    for stem in ("video_a", "video_b", "shots_a"):
        assert (out / stem / "analysis.json").exists(), stem
        assert (out / stem / "analysis.xlsx").exists(), stem

    state_path = folder / ".specto-watch-state.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert set(state) == {"video_a", "video_b", "shots_a"}
    assert all(entry["status"] == "ok" for entry in state.values())

    # second pass: nothing new, nothing touched -> zero processed
    rc = main(["watch", str(folder), "--out", str(out), "--fake", "--once"])
    assert rc == 0
    text = capsys.readouterr().out
    assert "0 processed, 3 skipped, 0 failed" in text


def test_watch_reprocesses_a_touched_source(synthetic_video: Path, tmp_path: Path, capsys) -> None:
    folder = tmp_path / "incoming"
    folder.mkdir()
    out = tmp_path / "out"

    video = folder / "video_a.mp4"
    shutil.copyfile(synthetic_video, video)
    (folder / "video_a.vtt").write_text(VTT, encoding="utf-8")

    assert main(["watch", str(folder), "--out", str(out), "--fake", "--once"]) == 0
    capsys.readouterr()
    analysis_path = out / "video_a" / "analysis.json"
    first_written = analysis_path.stat().st_mtime

    # nothing changed: a second pass skips it
    assert main(["watch", str(folder), "--out", str(out), "--fake", "--once"]) == 0
    assert "0 processed, 1 skipped, 0 failed" in capsys.readouterr().out
    assert analysis_path.stat().st_mtime == first_written

    # touch the source forward past the output's mtime -> reprocessed
    future = time.time() + 120
    os.utime(video, (future, future))
    assert main(["watch", str(folder), "--out", str(out), "--fake", "--once"]) == 0
    text = capsys.readouterr().out
    assert "1 processed, 0 skipped, 0 failed" in text
    assert "reprocessing video_a" in text
    assert analysis_path.stat().st_mtime > first_written


def test_watch_broken_item_is_logged_and_skipped_without_stopping_others(
    synthetic_video: Path, tmp_path: Path, capsys
) -> None:
    folder = tmp_path / "incoming"
    folder.mkdir()
    out = tmp_path / "out"

    good = folder / "good.mp4"
    shutil.copyfile(synthetic_video, good)
    (folder / "good.vtt").write_text(VTT, encoding="utf-8")

    broken = folder / "broken.mp4"
    broken.write_bytes(b"this is not a video file")

    _make_screenshot_folder(folder / "shots_a")

    rc = main(["watch", str(folder), "--out", str(out), "--fake", "--once"])
    assert rc == 0
    captured = capsys.readouterr()
    out_text, err_text = captured.out, captured.err
    combined = out_text + err_text
    assert "2 processed, 0 skipped, 1 failed" in out_text

    assert (out / "good" / "analysis.json").exists()
    assert (out / "shots_a" / "analysis.json").exists()
    assert not (out / "broken" / "analysis.json").exists()
    assert "FAILED broken" in combined

    state = json.loads((folder / ".specto-watch-state.json").read_text())
    assert state["broken"]["status"] == "error"
    assert state["broken"]["error"]
    assert state["good"]["status"] == "ok"
    assert state["shots_a"]["status"] == "ok"


def test_watch_once_with_nothing_in_the_folder(tmp_path: Path, capsys) -> None:
    folder = tmp_path / "incoming"
    folder.mkdir()
    out = tmp_path / "out"
    rc = main(["watch", str(folder), "--out", str(out), "--fake", "--once"])
    assert rc == 0
    assert "0 processed, 0 skipped, 0 failed" in capsys.readouterr().out
