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
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-not-real")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(doctor, "playwright_chromium_dir", lambda: "/Users/x/Library/Caches/ms-playwright/chromium-1234")
    monkeypatch.setattr(doctor, "system_name", lambda: "Darwin")
    monkeypatch.setattr(doctor, "screencapture_path", lambda: "/usr/sbin/screencapture")
    monkeypatch.setattr(doctor, "tool_path", lambda name: None)
    monkeypatch.setattr(doctor, "wayland_session", lambda: False)
    monkeypatch.setattr(doctor, "screen_recording_allowed", lambda: True)
    monkeypatch.setattr(doctor, "free_disk_bytes", lambda folder=".": 50 * 10**9)
    return cache


def test_everything_ok(healthy, capsys) -> None:
    checks = run_checks()
    assert all(c.ok for c in checks)
    names = [c.name for c in checks]
    assert names == ["Python", "ffmpeg", "faster-whisper", "stable-ts", "OCR",
                     "ANTHROPIC_API_KEY", "Gemini key", "Model access", "Playwright", "Screen capture", "Microphone input", "Disk space"]
    assert main() == 0
    out = capsys.readouterr().out
    assert out.count("\nok ") + out.startswith("ok ") == len(checks)
    assert not any(line.startswith("-- ") for line in out.splitlines())
    assert "Everything specto needs is here" in out
    assert "sk-ant" not in out  # the key is never printed
    assert "AIza" not in out


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


def _screen():
    return next(c for c in run_checks() if c.name == "Screen capture")


def test_screen_capture_on_a_mac(healthy) -> None:
    # healthy: mss not installed, screencapture present, permission granted
    sc = _screen()
    assert sc.ok and sc.detail.startswith("screencapture (/usr/sbin/screencapture)") and "live mode" in sc.detail
    assert "permission granted" in sc.detail
    doctor.module_installed = _installed("faster_whisper", "stable_whisper", "playwright", "mss")
    sc = _screen()
    assert sc.ok and sc.detail.startswith("mss (the mss library)") and "also present: screencapture" in sc.detail
    doctor.screen_recording_allowed = lambda: False
    sc = _screen()
    assert not sc.ok and "permission not granted" in sc.detail and "Screen Recording" in sc.fix
    doctor.screen_recording_allowed = lambda: None
    assert _screen().ok and "on first use" in _screen().detail
    doctor.screencapture_path = lambda: None
    doctor.module_installed = _installed("faster_whisper", "stable_whisper", "playwright")
    sc = _screen()
    assert not sc.ok and "none found on Darwin" in sc.detail
    assert 'specto[live]' in sc.fix and "/usr/sbin" in sc.fix
    assert main() == 0  # optional either way


def test_screen_capture_on_linux(healthy) -> None:
    doctor.system_name = lambda: "Linux"
    doctor.screencapture_path = lambda: None
    sc = _screen()
    assert not sc.ok and "none found on Linux" in sc.detail
    assert "grim" in sc.fix and "imagemagick" in sc.fix and 'specto[live]' in sc.fix
    doctor.tool_path = lambda name: "/usr/bin/import" if name == "import" else None
    sc = _screen()
    assert sc.ok and sc.detail.startswith("import (/usr/bin/import)")
    doctor.wayland_session = lambda: True
    doctor.tool_path = lambda name: f"/usr/bin/{name}"
    doctor.module_installed = _installed("faster_whisper", "stable_whisper", "playwright", "mss")
    sc = _screen()
    assert sc.ok and sc.detail.startswith("grim (/usr/bin/grim)") and "Wayland" in sc.detail
    assert "also present: mss, import" in sc.detail
    assert main() == 0


def test_screen_capture_on_windows(healthy) -> None:
    doctor.system_name = lambda: "Windows"
    doctor.screencapture_path = lambda: None
    sc = _screen()
    assert not sc.ok and "none found on Windows" in sc.detail and "powershell" in sc.fix.lower()
    doctor.tool_path = lambda name: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.EXE" if name == "powershell" else None
    sc = _screen()
    assert sc.ok and sc.detail.startswith("powershell (") and "permission" not in sc.detail
    assert main() == 0


def test_microphone_input_names_the_ffmpeg_input(healthy) -> None:
    mic = next(c for c in run_checks() if c.name == "Microphone input")
    assert mic.ok and "-f avfoundation -i :0" in mic.detail and "Microphone permission" in mic.detail
    doctor.system_name = lambda: "Windows"
    mic = next(c for c in run_checks() if c.name == "Microphone input")
    assert mic.ok and "-f dshow -i audio=<name>" in mic.detail and "first microphone" in mic.detail
    doctor.system_name = lambda: "Linux"
    mic = next(c for c in run_checks() if c.name == "Microphone input")
    assert mic.ok and "-f pulse -i default" in mic.detail and "hw:0" in mic.detail


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
    assert doctor.tool_path("no-such-tool-xyz") is None
    assert isinstance(doctor.wayland_session(), bool)
    assert doctor.screen_recording_allowed() in (True, False, None)


def test_gemini_key_both_ways(healthy, monkeypatch, capsys) -> None:
    key = next(c for c in run_checks() if c.name == "Gemini key")
    assert key.ok and key.detail.startswith("set")
    monkeypatch.delenv("GEMINI_API_KEY")
    key = next(c for c in run_checks() if c.name == "Gemini key")
    assert not key.ok and not key.required
    assert "not set" in key.detail and "aistudio.google.com" in key.fix
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-google-not-real")
    assert next(c for c in run_checks() if c.name == "Gemini key").ok
    monkeypatch.setenv("GOOGLE_API_KEY", "  ")
    assert not next(c for c in run_checks() if c.name == "Gemini key").ok
    assert main() == 0
    assert "AIza" not in capsys.readouterr().out


def _access(monkeypatch, claude: bool, gemini: bool):
    for name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    if claude:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    if gemini:
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-not-real")
    return next(c for c in run_checks() if c.name == "Model access")


def test_model_access_summary(healthy, monkeypatch, capsys) -> None:
    both = _access(monkeypatch, True, True)
    assert both.ok and both.detail == "Claude API and Gemini API"
    claude = _access(monkeypatch, True, False)
    assert claude.ok and claude.detail == "Claude API"
    gemini = _access(monkeypatch, False, True)
    assert gemini.ok and gemini.detail.startswith("Gemini API") and "--provider gemini" in gemini.detail
    none = _access(monkeypatch, False, False)
    assert not none.ok and not none.required
    assert none.detail == "no key: use specto demo, or specto requests / load to answer with any model you can reach"
    assert main() == 0  # no key is not a stop
    out = capsys.readouterr().out
    assert "-- Model access" in out and "specto demo" in out
