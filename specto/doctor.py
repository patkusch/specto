"""`specto doctor`: what is installed, what is missing, and what to do about it.

One line per check. Only two things must be there for specto to run at all,
Python 3.11 or newer and the ffmpeg binary that imageio-ffmpeg bundles;
everything else is optional and never fails the check, it just says what it
unlocks and how to get it. Nothing here downloads anything.

The lookups (module imports, the ffmpeg version, the Hugging Face cache, the
environment) go through small module-level helpers so tests can swap them.
"""
from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel

MIN_PYTHON = (3, 11)
WHISPER_MODEL_DOWNLOAD = "~150 MB"
MIN_FREE_DISK_GB = 1.0


class Check(BaseModel):
    name: str
    ok: bool
    detail: str
    fix: Optional[str] = None
    required: bool = False


# ---------------------------------------------------------------- lookups
# Each is a plain function so a test can replace it with monkeypatch.


def module_installed(name: str) -> bool:
    """True when `import name` would find the module (it is not imported)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def ffmpeg_path() -> Optional[str]:
    """The bundled ffmpeg binary, or None when imageio-ffmpeg cannot find one."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def ffmpeg_version(exe: str) -> Optional[str]:
    """'7.1' from `ffmpeg -version`, or None when it does not run."""
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    first = out.splitlines()[0] if out else ""
    parts = first.split()
    if len(parts) >= 3 and parts[0] == "ffmpeg" and parts[1] == "version":
        return parts[2]
    return first or None


def hf_cache_dir() -> Path:
    """Where Hugging Face keeps downloaded models, honouring its env vars."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"]).expanduser()
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]).expanduser() / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def whisper_model_cached(model_size: str = "base") -> Optional[str]:
    """The cached faster-whisper model folder for `model_size`, or None."""
    cache = hf_cache_dir()
    if not cache.is_dir():
        return None
    pattern = f"models--*faster-whisper-{model_size}"
    for entry in sorted(cache.glob(pattern)):
        if entry.is_dir():
            return entry.name
    return None


def ocr_installed() -> bool:
    from specto.ocr import ocr_available

    return ocr_available()


def api_key_set() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def gemini_key_set() -> bool:
    return any(os.environ.get(name, "").strip() for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"))


def playwright_chromium_dir() -> Optional[str]:
    """The Playwright chromium folder, or None when no browser has been installed."""
    candidates = []
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        candidates.append(Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]).expanduser())
    home = Path.home()
    candidates += [
        home / "Library" / "Caches" / "ms-playwright",
        home / ".cache" / "ms-playwright",
        home / "AppData" / "Local" / "ms-playwright",
    ]
    for base in candidates:
        if base.is_dir():
            for entry in sorted(base.glob("chromium-*")):
                if entry.is_dir():
                    return str(entry)
    return None


def system_name() -> str:
    return platform.system()


def screencapture_path() -> Optional[str]:
    return shutil.which("screencapture")


def tool_path(name: str) -> Optional[str]:
    """Where a command-line screenshot tool (grim, import, powershell) is, or None."""
    return shutil.which(name)


def wayland_session() -> bool:
    from specto.live import wayland_session as _wayland

    return _wayland()


def screen_recording_allowed() -> Optional[bool]:
    """macOS Screen Recording permission for this process: True, False, or None off macOS."""
    from specto.live import macos_screen_recording_allowed

    return macos_screen_recording_allowed()


def python_version() -> tuple[int, int, int]:
    return sys.version_info[:3]


def free_disk_bytes(folder: Path | str = ".") -> int:
    return shutil.disk_usage(str(folder)).free


# ----------------------------------------------------------------- checks


def check_python() -> Check:
    major, minor, micro = python_version()
    ok = (major, minor) >= MIN_PYTHON
    detail = f"{major}.{minor}.{micro}" + ("" if ok else f", needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer")
    return Check(name="Python", ok=ok, detail=detail, required=True,
                 fix=None if ok else f"install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ and rebuild the venv")


def check_ffmpeg() -> Check:
    exe = ffmpeg_path()
    if not exe:
        return Check(name="ffmpeg", ok=False, required=True, detail="not found",
                     fix='pip install "imageio-ffmpeg" (it bundles the binary)')
    version = ffmpeg_version(exe)
    if not version:
        return Check(name="ffmpeg", ok=False, required=True, detail=f"found at {exe} but it does not run",
                     fix='reinstall with pip install --force-reinstall "imageio-ffmpeg"')
    return Check(name="ffmpeg", ok=True, required=True, detail=f"version {version} at {exe}")


def check_whisper() -> Check:
    if not module_installed("faster_whisper"):
        return Check(name="faster-whisper", ok=False,
                     detail="not installed; needed only to transcribe audio when you have no transcript file",
                     fix='pip install "specto[whisper]"')
    cached = whisper_model_cached("base")
    if cached:
        return Check(name="faster-whisper", ok=True, detail=f"installed, base model already downloaded ({cached})")
    return Check(name="faster-whisper", ok=True,
                 detail=f"installed, base model not downloaded yet; will download {WHISPER_MODEL_DOWNLOAD} on first use")


def check_stable_ts() -> Check:
    if module_installed("stable_whisper"):
        return Check(name="stable-ts", ok=True, detail="installed; word times land about three times closer")
    return Check(name="stable-ts", ok=False,
                 detail="not installed (optional; word times still work, a little less precisely)",
                 fix='pip install "specto[whisper-precise]" (about 600 MB)')


def check_speakers() -> Check:
    if not module_installed("sherpa_onnx"):
        return Check(name="Speaker labels", ok=False,
                     detail="not installed; needed only for --speakers (who said what, from the audio)",
                     fix='pip install "specto[speakers]"')
    from specto.diarize import DEFAULT_MODEL_DIR, models_cached

    if models_cached():
        return Check(name="Speaker labels", ok=True, detail=f"installed, models already downloaded ({DEFAULT_MODEL_DIR})")
    return Check(name="Speaker labels", ok=True,
                 detail="installed, models not downloaded yet; --speakers will fetch ~33 MB on first use, no account needed")


def check_ocr() -> Check:
    try:
        available = ocr_installed()
    except Exception:
        available = False
    if available:
        return Check(name="OCR", ok=True, detail="installed; frame text is read and given to the model")
    return Check(name="OCR", ok=False, detail="not installed (optional; small labels may be missed)",
                 fix='pip install "specto[ocr]"')


def check_api_key() -> Check:
    if api_key_set():
        return Check(name="ANTHROPIC_API_KEY", ok=True, detail="set")
    return Check(name="ANTHROPIC_API_KEY", ok=False,
                 detail="not set; needed for a real run (--fake works without it)",
                 fix="export ANTHROPIC_API_KEY=... in your shell")


def check_gemini_key() -> Check:
    if gemini_key_set():
        return Check(name="Gemini key", ok=True, detail="set (GEMINI_API_KEY or GOOGLE_API_KEY)")
    return Check(name="Gemini key", ok=False,
                 detail="not set (optional; only for --provider gemini)",
                 fix="export GEMINI_API_KEY=... (a free key comes from https://aistudio.google.com/apikey)")


NO_KEY_ADVICE = "no key: use specto demo, or specto requests / load to answer with any model you can reach"


def check_model_access() -> Check:
    """What a run can call from here, going by which keys are set. Never prints a key."""
    reachable = [name for name, present in (("Claude API", api_key_set()), ("Gemini API", gemini_key_set())) if present]
    if not reachable:
        return Check(name="Model access", ok=False, detail=NO_KEY_ADVICE)
    detail = " and ".join(reachable)
    if reachable == ["Gemini API"]:
        detail += " (pass --provider gemini)"
    return Check(name="Model access", ok=True, detail=detail)


def check_playwright() -> Check:
    if not module_installed("playwright"):
        return Check(name="Playwright", ok=False, detail="not installed (optional, for rebuilding the example)",
                     fix="pip install playwright && playwright install chromium")
    chromium = playwright_chromium_dir()
    if chromium:
        return Check(name="Playwright", ok=True, detail=f"installed with chromium (optional, for rebuilding the example)")
    return Check(name="Playwright", ok=False, detail="installed but chromium is missing (optional, for rebuilding the example)",
                 fix="playwright install chromium")


def check_screen_capture() -> Check:
    """Which screen grab backend live mode will use here: mss, screencapture, grim, import, powershell or none."""
    from specto.live import INSTALL_HINTS, normalize_platform, screenshot_backends

    system = system_name()
    platform = normalize_platform(system)
    wayland = wayland_session() if platform == "linux" else False
    order = screenshot_backends(platform, wayland=wayland)
    found: list[tuple[str, str]] = []
    for name in order:
        if name == "mss":
            if module_installed("mss"):
                found.append((name, "the mss library"))
        elif name == "screencapture":
            exe = screencapture_path()
            if exe:
                found.append((name, exe))
        else:
            exe = tool_path(name)
            if exe:
                found.append((name, exe))
    if not found:
        fixes = " or ".join(INSTALL_HINTS[name] for name in order)
        return Check(name="Screen capture", ok=False,
                     detail=f"none found on {system} (optional; only live mode needs it)", fix=fixes)
    first, where = found[0]
    detail = f"{first} ({where}) will grab the screen in live mode"
    if len(found) > 1:
        detail += "; also present: " + ", ".join(name for name, _ in found[1:])
    if platform == "darwin":
        allowed = screen_recording_allowed()
        if allowed is True:
            detail += "; Screen Recording permission granted"
        elif allowed is False:
            return Check(name="Screen capture", ok=False, detail=detail + "; Screen Recording permission not granted",
                         fix="System Settings > Privacy & Security > Screen Recording, switch on your terminal app")
        else:
            detail += "; macOS asks for Screen Recording permission on first use"
    elif platform == "linux" and wayland:
        detail += " (Wayland session)"
    return Check(name="Screen capture", ok=True, detail=detail)


def check_microphone_input() -> Check:
    """Which ffmpeg input records the microphone on this platform."""
    from specto.live import DEFAULT_AUDIO_DEVICE, audio_input_args, normalize_platform

    platform = normalize_platform(system_name())
    if platform == "win32":
        detail = ("ffmpeg -f dshow -i audio=<name>; the first microphone ffmpeg lists is used unless "
                  "--audio-device names one; Windows must allow desktop apps to use the microphone")
    else:
        args = " ".join(audio_input_args(platform, DEFAULT_AUDIO_DEVICE))
        detail = f"ffmpeg {args}"
        if platform == "darwin":
            detail += "; macOS asks for Microphone permission on first use"
        else:
            detail += " (PulseAudio or PipeWire; pass --audio-device hw:0 for ALSA)"
    return Check(name="Microphone input", ok=True, detail=detail)


def check_disk(folder: Path | str = ".") -> Check:
    try:
        free = free_disk_bytes(folder)
    except Exception:
        return Check(name="Disk space", ok=False, detail="could not read free space for the current folder")
    free_gb = free / 1e9
    ok = free_gb >= MIN_FREE_DISK_GB
    detail = f"{free_gb:.1f} GB free in the current folder"
    if not ok:
        detail += f"; a run writes frames here and wants at least {MIN_FREE_DISK_GB:g} GB"
    return Check(name="Disk space", ok=ok, detail=detail, fix=None if ok else "free some space or use --out on another disk")


CHECKS: list[Callable[[], Check]] = [
    check_python,
    check_ffmpeg,
    check_whisper,
    check_stable_ts,
    check_speakers,
    check_ocr,
    check_api_key,
    check_gemini_key,
    check_model_access,
    check_playwright,
    check_screen_capture,
    check_microphone_input,
    check_disk,
]


def run_checks() -> list[Check]:
    """Every check, in the order they are printed."""
    return [check() for check in CHECKS]


def format_checks(checks: list[Check]) -> str:
    """One line per check: 'ok ' or '-- ', the name, what was found, and the fix if any."""
    width = max((len(c.name) for c in checks), default=0)
    lines = []
    for check in checks:
        prefix = "ok " if check.ok else "-- "
        line = f"{prefix}{check.name.ljust(width)}  {check.detail}"
        if not check.ok and check.fix:
            line += f"  ->  {check.fix}"
        lines.append(line)
    missing_required = [c.name for c in checks if c.required and not c.ok]
    if missing_required:
        lines.append(f"specto cannot run until this is fixed: {', '.join(missing_required)}.")
    else:
        lines.append("Everything specto needs is here. Lines marked -- are optional.")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None, extra_checks: Optional[list[Check]] = None) -> int:
    """Print the checks; exit 0 when everything required is ok, 1 otherwise.
    `extra_checks` are lines the caller worked out itself (the project settings file)."""
    checks = run_checks() + list(extra_checks or [])
    print(format_checks(checks))
    return 0 if all(c.ok for c in checks if c.required) else 1


if __name__ == "__main__":
    sys.exit(main())
