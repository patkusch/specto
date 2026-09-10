"""Build the onboarding example recording: walkthrough.mp4 and walkthrough.vtt.

What it does, in order:

1. Renders each page of the fake app in `app/` to a 1280x720 PNG. Playwright
   (headless Chromium) is tried first; if it is not installed or cannot start,
   the same screens are drawn with Pillow instead.
2. Speaks each line of the narration with the Mac's `say` command and measures
   how long each clip is with ffmpeg. If `say` is missing, silence of a guessed
   length is used instead so the video and the VTT are still built.
3. Lays the lines out on a timeline, screen by screen, with a short pause after
   each line, joins the audio, makes the video from the PNGs with one entry per
   screen, muxes the two, and writes a WebVTT file from the same timeline.

Run it from the repo root with the project venv:

    .venv/bin/python examples/onboarding/make_example.py

Outputs land next to this file. Nothing here touches the specto package.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import imageio_ffmpeg

HERE = Path(__file__).resolve().parent
APP = HERE / "app"
WORK = HERE / "build"
FRAMES = HERE / "frames"
MP4 = HERE / "walkthrough.mp4"
VTT = HERE / "walkthrough.vtt"

WIDTH, HEIGHT = 1280, 720
PAUSE = 0.6  # seconds of silence after every line
LEAD_IN = 0.8  # silence before the first line
VOICE = "Samantha"
SAMPLE_RATE = 22050

# (screen name, html file, [narration lines]). The screen shown on video is the
# one whose lines are being spoken. Each line is what an expert would say.
SCRIPT: list[tuple[str, str, list[str]]] = [
    ("Customer Search", "search.html", [
        "So this is Northwind Onboarding, the tool we use to set up new customers. The first screen is Customer Search.",
        "You put in the postcode and the surname and hit Search, and it lists anyone we already have, so you don't create a duplicate.",
    ]),
    ("Customer Details", "details.html", [
        "If they're new, you go to Customer Details. First name, last name, date of birth, account type and postcode are mandatory; the rest we fill in when we have it.",
        "The postcode has to be a valid UK format, otherwise the system won't let you save. The address lines and the town are optional.",
        "Account type is a dropdown, personal or business, and the marketing consent box only gets ticked if the customer actually said yes. Then you press Save and continue.",
    ]),
    ("Documents", "documents.html", [
        "Next is Documents. We upload a photo of their ID, usually a passport or a driving licence, and pick the document type from the dropdown.",
        "The status column shows pending until the document has been checked. Someone checks them, I'm honestly not sure who, it just changes to verified at some point.",
    ]),
    ("Review & Submit", "review.html", [
        "Review and Submit is a summary of everything so far. You read it through, and if it all looks right you press Submit for approval.",
        "Once you submit, the record locks and you can't edit it any more. You'd have to ask a team lead to send it back, I think.",
    ]),
    ("Approval Queue", "queue.html", [
        "This is the Approval Queue. Every submitted customer sits here with a status, and it shows who it's assigned to, though I've never worked out how it picks the person.",
        "Only team leads can approve. The Approve button doesn't show for the rest of us, and it says team leads only at the top.",
    ]),
    ("Confirmation", "confirmation.html", [
        "When it's approved, you get the Confirmation screen with a reference number. We give that reference to the customer.",
        "A welcome email goes out automatically to the address we entered. If the email bounces it just sort of sits there, nobody gets told.",
        "And that's the whole onboarding process, start to finish.",
    ]),
]

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace")


# ------------------------------------------------------------------ rendering


def render_with_playwright() -> bool:
    """Screenshot every page with headless Chromium. False if that is not possible."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("render: playwright is not installed")
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
            for index, (_, html, _) in enumerate(SCRIPT):
                page.goto((APP / html).resolve().as_uri())
                page.wait_for_load_state("load")
                page.screenshot(path=str(FRAMES / f"screen_{index:02d}.png"))
            browser.close()
    except Exception as exc:  # browser missing, sandbox, etc.
        print(f"render: playwright could not run ({type(exc).__name__}: {exc})")
        return False
    return True


def render_with_pillow() -> None:
    """Draw the same six screens without a browser: header, nav, title, fields."""
    from PIL import Image, ImageDraw, ImageFont

    def font(size: int, bold: bool = False):
        for name in (["Helvetica-Bold", "Arial Bold"] if bold else ["Helvetica", "Arial"]):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    # Rough content per screen: labels of inputs (with required marker) and table headers.
    content = {
        "search.html": (["Postcode", "Surname"], ["Customer ID", "Name", "Date of birth", "Postcode", "Account type", "Status"], "Search", None),
        "details.html": (["First name*", "Last name*", "Date of birth*", "Email address", "Phone number", "Account type*", "Address line 1", "Address line 2", "Town / City", "Postcode*"], [], "Save and continue", "[ ] Customer has given marketing consent"),
        "documents.html": (["ID document*", "Document type*"], ["File", "Document type", "Uploaded", "Uploaded by", "Status"], "Continue to review", None),
        "review.html": ([], ["Name", "Priya Shah"], "Submit for approval", None),
        "queue.html": ([], ["Reference", "Customer", "Submitted", "Status", "Assigned to", "Action"], "Approve", "Team leads only. The Approve action is shown to users in the Team Lead role."),
        "confirmation.html": ([], [], "Start another customer", "Customer reference number  NW-2026-04417"),
    }
    values = {"First name*": "Priya", "Last name*": "Shah", "Date of birth*": "14/06/1988", "Email address": "priya.shah@example.com",
              "Phone number": "07700 900123", "Account type*": "Personal", "Address line 1": "14 Pembridge Gardens", "Address line 2": "Flat 3",
              "Town / City": "London", "Postcode*": "SW1A 1AA", "Postcode": "SW1A 1AA", "Surname": "Shah", "Document type*": "Passport", "ID document*": "Choose file"}
    for index, (name, html, _) in enumerate(SCRIPT):
        img = Image.new("RGB", (WIDTH, HEIGHT), (243, 245, 247))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, WIDTH, 52], fill=(30, 58, 95))
        d.rectangle([20, 15, 42, 37], fill=(246, 166, 35))
        d.text((52, 14), "Northwind Onboarding", fill="white", font=font(20, True))
        d.text((980, 18), "Signed in as Jo Patel - Onboarding Officer", fill="white", font=font(13))
        d.rectangle([0, 52, 220, HEIGHT - 28], fill="white", outline=(223, 227, 232))
        for i, (nav_name, _, _) in enumerate(SCRIPT):
            y = 80 + i * 38
            if i == index:
                d.rectangle([0, y - 8, 220, y + 26], fill=(238, 243, 248))
                d.rectangle([0, y - 8, 3, y + 26], fill=(30, 58, 95))
            d.ellipse([20, y, 40, y + 20], fill=(30, 58, 95) if i == index else (46, 139, 87) if i < index else (223, 227, 232))
            d.text((26, y + 3), str(i + 1), fill="white" if i <= index else (82, 96, 109), font=font(11))
            d.text((50, y + 2), nav_name.replace("&", "&"), fill=(30, 58, 95) if i == index else (51, 78, 104), font=font(14, i == index))
        d.text((252, 76), name, fill=(31, 41, 51), font=font(24, True))
        labels, headers, button, note = content[html]
        y = 130
        if note:
            d.rectangle([252, y, 1240, y + 30], fill=(255, 248, 225), outline=(240, 215, 140))
            d.text((262, y + 8), note, fill=(107, 84, 0), font=font(13))
            y += 44
        if labels:
            d.rectangle([252, y, 1240, y + 40 + 62 * ((len(labels) + 1) // 2)], fill="white", outline=(223, 227, 232))
            for i, label in enumerate(labels):
                x = 276 + (i % 2) * 480
                ly = y + 18 + (i // 2) * 62
                plain = label.rstrip("*")
                d.text((x, ly), plain, fill=(62, 76, 89), font=font(12, True))
                if label.endswith("*"):
                    w = d.textlength(plain, font=font(12, True))
                    d.text((x + w + 2, ly), "*", fill=(214, 69, 69), font=font(12, True))
                d.rectangle([x, ly + 18, x + 440, ly + 50], fill="white", outline=(184, 194, 204))
                d.text((x + 10, ly + 26), values.get(label, ""), fill=(31, 41, 51), font=font(14))
            y += 60 + 62 * ((len(labels) + 1) // 2)
        if headers:
            d.rectangle([252, y, 1240, y + 150], fill="white", outline=(223, 227, 232))
            for i, h in enumerate(headers):
                d.text((272 + i * 160, y + 14), h.upper(), fill=(123, 135, 148), font=font(11))
            d.line([262, y + 36, 1230, y + 36], fill=(223, 227, 232), width=2)
            y += 166
        bw = d.textlength(button, font=font(14, True)) + 36
        d.rectangle([252, y + 10, 252 + bw, y + 46], fill=(30, 58, 95))
        d.text((270, y + 20), button, fill="white", font=font(14, True))
        d.rectangle([0, HEIGHT - 28, WIDTH, HEIGHT], fill="white", outline=(228, 231, 235))
        d.text((32, HEIGHT - 21), "Northwind Onboarding v4.2 - Internal use only", fill=(154, 165, 177), font=font(11))
        img.save(FRAMES / f"screen_{index:02d}.png")


# ---------------------------------------------------------------------- audio


def clip_duration(path: Path) -> float:
    """Length of an audio file in seconds, read from ffmpeg's `Duration:` line."""
    stderr = run([ffmpeg(), "-hide_banner", "-i", str(path)]).stderr
    match = _DURATION_RE.search(stderr)
    if not match:
        raise RuntimeError(f"ffmpeg gave no duration for {path}")
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def speak(text: str, out: Path) -> bool:
    """Say `text` into a 16-bit 22050 Hz mono wav. False if `say` is unavailable."""
    if shutil.which("say") is None:
        return False
    result = run(["say", "-v", VOICE, "-o", str(out), f"--data-format=LEI16@{SAMPLE_RATE}", text])
    if result.returncode != 0:
        result = run(["say", "-o", str(out), f"--data-format=LEI16@{SAMPLE_RATE}", text])
    return result.returncode == 0 and out.exists() and out.stat().st_size > 0


def silence(seconds: float, out: Path) -> None:
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(b"\x00\x00" * int(seconds * SAMPLE_RATE))


def join_audio(parts: list[tuple[Path, float]], out: Path) -> float:
    """Write `parts` (file, trailing silence seconds) end to end into one wav."""
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(b"\x00\x00" * int(LEAD_IN * SAMPLE_RATE))
        for path, gap in parts:
            with wave.open(str(path), "rb") as r:
                assert r.getnchannels() == 1 and r.getsampwidth() == 2 and r.getframerate() == SAMPLE_RATE, path
                w.writeframes(r.readframes(r.getnframes()))
            w.writeframes(b"\x00\x00" * int(gap * SAMPLE_RATE))
        total = w.getnframes() / SAMPLE_RATE
    return total


# ---------------------------------------------------------------------- build


def vtt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def main() -> int:
    WORK.mkdir(exist_ok=True)
    FRAMES.mkdir(exist_ok=True)

    if render_with_playwright():
        renderer = "playwright"
    else:
        render_with_pillow()
        renderer = "pillow"
    print(f"render: {len(SCRIPT)} screens drawn with {renderer} -> {FRAMES}")

    # Speak every line and measure it.
    have_voice = True
    clips: list[list[tuple[Path, float]]] = []  # per screen: (wav, duration)
    n = 0
    for _, _, lines in SCRIPT:
        screen_clips = []
        for text in lines:
            n += 1
            wav = WORK / f"line_{n:02d}.wav"
            if have_voice and not speak(text, wav):
                have_voice = False
            if not have_voice:
                silence(max(2.0, 0.35 * len(text.split())), wav)
            screen_clips.append((wav, clip_duration(wav)))
        clips.append(screen_clips)
    if not have_voice:
        print("audio: `say` is not available here, so the narration is silent")

    # Timeline: screens in order, each line followed by a pause.
    cues: list[tuple[float, float, str, str]] = []  # start, end, screen, text
    screen_spans: list[tuple[str, float, float]] = []
    t = LEAD_IN
    audio_parts: list[tuple[Path, float]] = []
    for (screen, _, lines), screen_clips in zip(SCRIPT, clips):
        screen_start = t
        for text, (wav, dur) in zip(lines, screen_clips):
            cues.append((t, t + dur, screen, text))
            audio_parts.append((wav, PAUSE))
            t += dur + PAUSE
        screen_spans.append((screen, screen_start, t))
    audio_wav = WORK / "narration.wav"
    total = join_audio(audio_parts, audio_wav)
    # The first screen also covers the lead-in silence.
    screen_spans[0] = (screen_spans[0][0], 0.0, screen_spans[0][2])
    screen_spans[-1] = (screen_spans[-1][0], screen_spans[-1][1], total)

    print("\ntimeline:")
    for i, (screen, start, end) in enumerate(screen_spans):
        print(f"  {start:6.2f} - {end:6.2f}  screen_{i:02d}  {screen}")
        for c_start, c_end, c_screen, text in cues:
            if c_screen == screen:
                print(f"           {c_start:6.2f} - {c_end:6.2f}    {text}")
    print(f"  total {total:.2f}s\n")

    # Video: one concat entry per screen with its duration, then mux with audio.
    concat = WORK / "screens.txt"
    lines_out = []
    for i, (_, start, end) in enumerate(screen_spans):
        lines_out.append(f"file '{(FRAMES / f'screen_{i:02d}.png').resolve()}'")
        lines_out.append(f"duration {end - start:.3f}")
    lines_out.append(f"file '{(FRAMES / f'screen_{len(screen_spans) - 1:02d}.png').resolve()}'")  # concat quirk: last entry needs repeating
    concat.write_text("\n".join(lines_out) + "\n", encoding="utf-8")

    encoders = run([ffmpeg(), "-hide_banner", "-encoders"]).stdout
    codec = "libx264" if "libx264" in encoders else "mpeg4"
    cmd = [
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat),
        "-i", str(audio_wav),
        "-c:v", codec, "-pix_fmt", "yuv420p", "-r", "2",
        *(["-crf", "24", "-tune", "stillimage"] if codec == "libx264" else ["-q:v", "5"]),
        "-c:a", "aac", "-b:a", "64k", "-ar", "22050",
        "-t", f"{total:.3f}",
        str(MP4),
    ]
    result = run(cmd)
    if result.returncode != 0:
        print(result.stderr)
        return 1

    # WebVTT from the same timeline, one cue per line, spoken by "Expert".
    out = ["WEBVTT", ""]
    for i, (start, end, _, text) in enumerate(cues, 1):
        out += [str(i), f"{vtt_time(start)} --> {vtt_time(end)}", f"<v Expert>{text}", ""]
    VTT.write_text("\n".join(out), encoding="utf-8")

    size_mb = MP4.stat().st_size / 1_000_000
    print(f"wrote {MP4.name} ({size_mb:.2f} MB, {codec}, {total:.1f}s) and {VTT.name} ({len(cues)} cues), renderer {renderer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
