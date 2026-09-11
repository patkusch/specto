"""Tests for `specto doctor`.

Every lookup is swapped with monkeypatch so each check is driven to both
outcomes; nothing here downloads or imports the heavy optional libraries.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from specto import doctor
from specto.doctor import Check, format_checks, main, run_checks


def _installed(*names: str):
    return lambda name: name in names


@pytest.fixture
def healthy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Everything present; tests turn single things off from here."""
    monkeypatch.setattr(doctor, "python_version", lambda: (3, 12, 4))
    monkeypatch.setattr(doctor, "ffmpeg_path", lambda: "/venv/lib/imageio_ffmpeg/binaries/ffmpeg")
    monkeypatch.setattr(doctor, "ffmpeg_version", lambda exe: "7.1")
    monkeypatch.setattr(doctor, "module_installed", _installed("faster_whisper", "stable_whisper", "playwright"))
    cache = tmp_path / "hub"
    (cache / "models--Systran--faster-whisper-base").mkdir(parents=True)
    monkeypatch.setattr(doctor, "hf_cache_dir", lambda: cache)
    monkeypatch.setattr(doctor, "ocr_installed", lambda: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    monkeypatch.setattr(doctor, "playwright_chromium_dir", lambda: "/Users/x/Library/Caches/ms-playwright/chromium-1234")
    monkeypatch.setattr(doctor, "system_name", lambda: "Darwin")
    monkeypatch.setattr(doctor, "screencapture_path", lambda: "/usr/sbin/screencapture")
    monkeypatch.setattr(doctor, "free_disk_bytes", lambda folder=".": 50 * 10**9)
    return cache


def test_everything_ok(healthy, capsys) -> None:
    checks = run_checks()
    assert all(c.ok for c in checks)
    names = [c.name for c in checks]
    assert names == ["Python", "ffmpeg", "faster-whisper", "stable-ts", "OCR",
                     "ANTHROPIC_API_KEY", "Playwright", "screencapture", "Disk space"]
    assert main() == 0
    out = capsys.readouterr().out
    assert out.count("\nok ") + out.startswith("ok ") == len(checks)
    assert not any(line.startswith("-- ") for line in out.splitlines())
    assert "Everything specto needs is here" in out
    assert "sk-ant" not in out  # the key is never printed


def test_python_too_old_fails(healthy, capsys) -> None:
    doctor.python_version = lambda: (3, 9, 1)
    checks = run_checks()
    python = next(c for c in checks if c.name == "Python")
    assert not python.ok and python.required
    assert "needs 3.11" in python.detail
    assert main() == 1
    assert "cannot run until this is fixed: Python" in capsys.readouterr().out


def test_ffmpeg_missing_fails(healthy) -> None:
    doctor.ffmpeg_path = lambda: None
    ffmpeg = next(c for c in run_checks() if c.name == "ffmpeg")
    assert not ffmpeg.ok and ffmpeg.required
    assert "not found" in ffmpeg.detail
    assert "imageio-ffmpeg" in ffmpeg.fix
    assert main() == 1


def test_ffmpeg_present_but_broken_fails(healthy) -> None:
    doctor.ffmpeg_version = lambda exe: None
    ffmpeg = next(c for c in run_checks() if c.name == "ffmpeg")
    assert not ffmpeg.ok
    assert "does not run" in ffmpeg.detail
    assert main() == 1


def test_ffmpeg_ok_reports_version(healthy) -> None:
    ffmpeg = next(c for c in run_checks() if c.name == "ffmpeg")
    assert ffmpeg.ok
    assert "7.1" in ffmpeg.detail and "/binaries/ffmpeg" in ffmpeg.detail


def test_ffmpeg_version_parses_real_banner(monkeypatch) -> None:
    class Result:
        stdout = "ffmpeg version 7.1-static https://johnvansickle.com/ffmpeg/\nbuilt with gcc\n"

    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: Result())
    assert doctor.ffmpeg_version("/any/ffmpeg") == "7.1-static"

    def boom(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr(doctor.subprocess, "run", boom)
    assert doctor.ffmpeg_version("/any/ffmpeg") is None


def test_whisper_not_installed_is_optional(healthy) -> None:
    doctor.module_installed = _installed("stable_whisper", "playwright")
    whisper = next(c for c in run_checks() if c.name == "faster-whisper")
    assert not whisper.ok and not whisper.required
    assert "specto[whisper]" in whisper.fix
    assert main() == 0


def test_whisper_model_not_downloaded_yet(healthy: Path) -> None:
    import shutil

    shutil.rmtree(healthy / "models--Systran--faster-whisper-base")
    whisper = next(c for c in run_checks() if c.name == "faster-whisper")
    assert whisper.ok
    assert "will download ~150 MB on first use" in whisper.detail


def test_whisper_model_downloaded(healthy) -> None:
    whisper = next(c for c in run_checks() if c.name == "faster-whisper")
    assert whisper.ok
    assert "already downloaded" in whisper.detail
    assert "models--Systran--faster-whisper-base" in whisper.detail


def test_hf_cache_dir_honours_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    assert doctor.hf_cache_dir() == Path.home() / ".cache" / "huggingface" / "hub"
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    assert doctor.hf_cache_dir() == tmp_path / "hf" / "hub"
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    assert doctor.hf_cache_dir() == tmp_path / "hub"
    assert doctor.whisper_model_cached("base") is None  # folder does not exist


def test_stable_ts_both_ways(healthy) -> None:
    assert next(c for c in run_checks() if c.name == "stable-ts").ok
    doctor.module_installed = _installed("faster_whisper", "playwright")
    stable = next(c for c in run_checks() if c.name == "stable-ts")
    assert not stable.ok and not stable.required
    assert "whisper-precise" in stable.fix
    assert main() == 0


def test_ocr_both_ways(healthy) -> None:
    assert next(c for c in run_checks() if c.name == "OCR").ok
    doctor.ocr_installed = lambda: False
    ocr = next(c for c in run_checks() if c.name == "OCR")
    assert not ocr.ok and "specto[ocr]" in ocr.fix
    assert main() == 0

    def boom():
        raise RuntimeError("engine exploded")

    doctor.ocr_installed = boom
    assert not next(c for c in run_checks() if c.name == "OCR").ok


def test_api_key_both_ways(healthy, monkeypatch, capsys) -> None:
    key = next(c for c in run_checks() if c.name == "ANTHROPIC_API_KEY")
    assert key.ok and key.detail == "set"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    key = next(c for c in run_checks() if c.name == "ANTHROPIC_API_KEY")
    assert not key.ok and not key.required
    assert "not set" in key.detail and "--fake" in key.detail
    assert main() == 0
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    assert not next(c for c in run_checks() if c.name == "ANTHROPIC_API_KEY").ok


def test_playwright_three_states(healthy) -> None:
    pw = next(c for c in run_checks() if c.name == "Playwright")
    assert pw.ok and "optional, for rebuilding the example" in pw.detail
    doctor.playwright_chromium_dir = lambda: None
    pw = next(c for c in run_checks() if c.name == "Playwright")
    assert not pw.ok and pw.fix == "playwright install chromium"
    doctor.module_installed = _installed("faster_whisper", "stable_whisper")
    pw = next(c for c in run_checks() if c.name == "Playwright")
    assert not pw.ok and "pip install playwright" in pw.fix
    assert main() == 0


def test_screencapture_states(healthy) -> None:
    sc = next(c for c in run_checks() if c.name == "screencapture")
    assert sc.ok and "live mode" in sc.detail
    doctor.screencapture_path = lambda: None
    sc = next(c for c in run_checks() if c.name == "screencapture")
    assert not sc.ok and sc.fix
    doctor.system_name = lambda: "Linux"
    sc = next(c for c in run_checks() if c.name == "screencapture")
    assert not sc.ok and "Linux" in sc.detail and sc.fix is None
    assert main() == 0


def test_disk_space_states(healthy) -> None:
    disk = next(c for c in run_checks() if c.name == "Disk space")
    assert disk.ok and "50.0 GB free" in disk.detail
    doctor.free_disk_bytes = lambda folder=".": 200 * 10**6
    disk = next(c for c in run_checks() if c.name == "Disk space")
    assert not disk.ok and "0.2 GB free" in disk.detail and "--out" in disk.fix
    assert main() == 0  # low disk is a warning, not a stop

    def boom(folder="."):
        raise OSError("no")

    doctor.free_disk_bytes = boom
    assert not next(c for c in run_checks() if c.name == "Disk space").ok


def test_format_checks_layout() -> None:
    checks = [
        Check(name="ffmpeg", ok=True, detail="version 7.1 at /x/ffmpeg", required=True),
        Check(name="OCR", ok=False, detail="not installed (optional)", fix='pip install "specto[ocr]"'),
        Check(name="Disk space", ok=False, detail="0.2 GB free"),
    ]
    text = format_checks(checks)
    lines = text.splitlines()
    assert lines[0].startswith("ok ffmpeg")
    assert lines[1].startswith("-- OCR")
    assert '->  pip install "specto[ocr]"' in lines[1]
    assert lines[2].startswith("-- Disk space") and "->" not in lines[2]
    assert lines[-1] == "Everything specto needs is here. Lines marked -- are optional."
    assert not any(ord(ch) > 127 for ch in text)  # plain ASCII, no emoji

    checks[0] = Check(name="ffmpeg", ok=False, detail="not found", fix="pip install imageio-ffmpeg", required=True)
    assert format_checks(checks).splitlines()[-1] == "specto cannot run until this is fixed: ffmpeg."
    assert format_checks([]).endswith("optional.")


def test_real_lookups_do_not_raise() -> None:
    """The real helpers run on this machine without touching the network."""
    assert isinstance(doctor.module_installed("os"), bool)
    assert doctor.module_installed("no_such_module_xyz") is False
    assert isinstance(doctor.python_version(), tuple)
    assert doctor.free_disk_bytes(".") > 0
    assert isinstance(doctor.system_name(), str)
    assert doctor.whisper_model_cached("no-such-size") is None
