"""End to end: the command line runs every stage on a synthetic video with the stand-in model."""
from __future__ import annotations

from pathlib import Path

import openpyxl

from specto.cli import main
from specto.model import Analysis, Recording

VTT = """WEBVTT

00:00:00.000 --> 00:00:02.500
<v Sam>This is the customer search screen, we type the postcode here.

00:00:03.200 --> 00:00:05.800
<v Sam>Then we open the customer details and check the account number.

00:00:06.100 --> 00:00:08.700
<v Sam>Save sends it to the approval queue, only a team lead can approve.

00:00:09.300 --> 00:00:11.500
<v Sam>And that is it, the customer is done.
"""


def test_cli_run_with_fake_model(synthetic_video: Path, tmp_path: Path) -> None:
    vtt = tmp_path / "walkthrough.vtt"
    vtt.write_text(VTT, encoding="utf-8")
    out = tmp_path / "out"

    rc = main(["run", str(synthetic_video), "--transcript", str(vtt), "--out", str(out), "--fake"])
    assert rc == 0

    for name in ("recording.json", "analysis.json", "analysis.xlsx", "report.md"):
        assert (out / name).exists(), name
    recording = Recording.model_validate_json((out / "recording.json").read_text())
    analysis = Analysis.model_validate_json((out / "analysis.json").read_text())
    assert recording.transcript_source == "file"
    assert len(recording.segments) == 4
    assert analysis.requirements and analysis.questions
    for kf in recording.keyframes:
        assert (out / kf.path).exists()

    wb = openpyxl.load_workbook(out / "analysis.xlsx")
    assert wb.sheetnames[0] == "Summary"
    assert "SME Questions" in wb.sheetnames

    # second run reuses the saved stages and still exports
    rc = main(["run", str(synthetic_video), "--transcript", str(vtt), "--out", str(out), "--fake"])
    assert rc == 0

    # export alone rebuilds the workbook from the saved analysis
    (out / "analysis.xlsx").unlink()
    assert main(["export", str(out)]) == 0
    assert (out / "analysis.xlsx").exists()


def test_cli_refuses_without_key(synthetic_video: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    vtt = tmp_path / "w.vtt"
    vtt.write_text(VTT, encoding="utf-8")
    rc = main(["run", str(synthetic_video), "--transcript", str(vtt), "--out", str(tmp_path / "o")])
    assert rc == 2
