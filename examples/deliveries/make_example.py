"""Build the deliveries example recording: walkthrough.mp4 and walkthrough.vtt.

The third standing example, and the one meant as a held-out test. It differs
from the other two in shape: a PORTRAIT phone-app screen recording (540x1170)
of an invented courier driver app, "Harbourline Drivers", not a landscape
desktop. No prompt in specto was ever tuned against it, and its answer key
(expected.json) was written from the narration script and screen designs in
this file before any model read the recording.

What it does, in order:

1. Generates every sample value (driver, supervisor and recipient names,
   parcel ids, street names, dates, amounts, phone numbers) from one seeded
   random generator, `random.Random(SEED)`, so the pages are the same on every
   rebuild and contain no real person, firm or postcode.
2. Writes the six phone screens into `app/` as plain HTML (plus style.css) and
   renders each with Playwright's mobile emulation: a 390x844 CSS viewport at
   device scale factor 2, so 780x1688 pixels, then resized to the 540x1170 the
   recording uses.
3. Speaks each narration line with the Mac's `say` command (Moira), measures
   the clips with ffmpeg, lays them out screen by screen with a 0.6 s pause
   after each line, joins the audio, builds the video from the frames, muxes the
   two, and writes a WebVTT file from the same timeline.

Run it from the repo root with the project venv:

    .venv/bin/python examples/deliveries/make_example.py

Outputs land next to this file. Nothing here touches the specto package.

Two things are on the screens but never said aloud, on purpose, and one thing
is said that a screen contradicts. They are what the answer key's
screen-only questions come from:

* The parcel HL-xxxxx on the Stop detail screen carries an orange "Hazardous"
  badge. The narration never mentions it.
* The narration says every stop has a phone number. The Stop detail screen
  shows "No number on file" and a greyed-out Call button.
"""
from __future__ import annotations

import html
import random
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import imageio_ffmpeg
from PIL import Image

HERE = Path(__file__).resolve().parent
APP = HERE / "app"
WORK = HERE / "build"
FRAMES = HERE / "frames"
MP4 = HERE / "walkthrough.mp4"
VTT = HERE / "walkthrough.vtt"

CSS_W, CSS_H = 390, 844  # the phone viewport in CSS pixels
SCALE = 2  # device scale factor, so 780x1688 real pixels before resizing
VIDEO_W, VIDEO_H = 540, 1170  # the recording: portrait, phone aspect
PAUSE = 0.6  # seconds of silence after every line
LEAD_IN = 0.8  # silence before the first line
VOICE = "Moira"
SAMPLE_RATE = 22050
SEED = 20260918

# The phone's status-bar clock on each screen, so the top-left corner changes
# from screen to screen as a real recording's would.
CLOCKS = ["08:52", "09:14", "09:31", "09:33", "09:40", "17:48"]

# (screen name, html file, [narration lines]). The screen shown on video is the
# one whose lines are being spoken. Each line is what a dispatcher would say to
# a new driver. Nothing here mentions the Hazardous badge, and line 5 says every
# stop has a phone number while the Stop detail screen shows one that has none.
SCRIPT: list[tuple[str, str, list[str]]] = [
    ("Today's runs", "runs.html", [
        "Right, welcome aboard. This is Harbourline Drivers, the app you'll live in all day. When you sign in you land on Today's runs, which is your six stops in the order we'd like you to drive them.",
        "Each stop has a little chip saying where it's at: next, pending, delivered or failed. Under the address there's a time window, the slot we promised the customer. The window shows red sometimes and I never worked out why, so I don't know what to tell you, just try to be there.",
        "Tap any stop to open it.",
    ]),
    ("Stop detail", "stop.html", [
        "Stop detail is the page for one drop. You get the address, how many parcels are going there, and any instructions the customer left, like ring twice or leave it round the back.",
        "There are two big buttons. Call rings the customer and Navigate opens the map. Every stop has a phone number on it, so you can always ring ahead if you're running late.",
        "Parcels over one hundred pounds need a photo and a signature, no exceptions. The value is printed next to each parcel so you can see straight away. When you're at the door, hit Proof of delivery.",
    ]),
    ("Proof of delivery", "proof.html", [
        "On Proof of delivery you take a photo of the parcel, type in the name of whoever took it, and get them to sign in the box with their finger.",
        "See the Delivered button? It stays grey until the photo is taken. Once the photo is in, it goes green and you can press it. That's the only way a stop ever becomes delivered.",
        "And if nobody's in, don't just leave it on the step. Tap the link at the bottom to report a failed delivery instead.",
    ]),
    ("Failed delivery", "failed.html", [
        "Failed delivery is dead simple. You pick a reason from the list: nobody home, refused, address not found, access blocked, or damaged. You have to pick one, it won't let you carry on without.",
        "There's a free-text box underneath for anything else, but honestly most people leave it blank.",
        "Three failed attempts and it goes back to the depot automatically. You don't do anything, the stop just drops off your list.",
    ]),
    ("Reassign stop", "reassign.html", [
        "Reassign stop is a supervisor screen, so you won't normally see it. Only supervisors can reassign a stop. You pick the new driver from the list and you have to write a reason, because it goes in the audit trail.",
        "If a van breaks down, it's me or one of the other supervisors who moves your stops across to someone else.",
    ]),
    ("End of day summary", "summary.html", [
        "Last one, End of day summary. It shows how many you delivered, how many failed, and the cash collected, that's for the cash-on-delivery parcels. The cash figure comes from somewhere in finance, I just take whatever it shows.",
        "You type your mileage in and press Submit day. You can't submit until every stop is either delivered or failed, so check nothing's still pending.",
        "And that's it. Anything you're stuck on, ring the depot.",
    ]),
]

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace")


# ------------------------------------------------------------------ sample data
#
# Every value on the pages is GENERATED by `sample_data`, seeded with SEED, so
# the example is the same on every rebuild. Names are glued together from
# syllables, so they are nobody's; streets come from a list of obvious
# placeholders; parcel ids are "HL-" plus digits; phone numbers are only in the
# 07700 900xxx range Ofcom reserves for fiction; dates are the first of a month;
# money is a round number. There are no postcodes and no real firms.

_FIRST_A = ["Ma", "Lo", "Ri", "Ta", "Ne", "Sa", "Vi", "Ka", "Jo", "El", "Da", "Fe"]
_FIRST_B = ["ra", "nel", "sha", "na", "vin", "lo", "mi", "dor", "ta", "ren", "lia", "bo"]
_SUR_A = ["Har", "Bel", "Cor", "Dun", "Fal", "Gra", "Mor", "Pen", "Ros", "Wal", "Ash", "Kel"]
_SUR_B = ["mond", "ley", "wick", "ford", "ston", "brook", "dale", "ridge", "ton", "by", "combe", "well"]
_STREETS = ["Example Street", "Placeholder Road", "Sample Avenue", "Test Lane", "Dummy Close", "Fictional Way",
            "Specimen Court", "Demo Terrace"]
_TOWN = "Sampleton"
_INSTRUCTIONS = [
    "Ring twice, the bell is quiet.",
    "Side gate is open. Leave it in the porch if nobody answers.",
    "Flat entrance is round the back, past the bins.",
    "Please knock, the baby is asleep.",
    "Dog in the garden, keep the gate shut.",
    "Concierge desk is open until five.",
]
# Time windows are fixed, one per stop, two hours each.
_WINDOWS = [("09:00", "11:00"), ("09:30", "11:30"), ("11:00", "13:00"), ("12:00", "14:00"), ("13:00", "15:00"), ("15:00", "17:00")]
_REASONS = ["Nobody home", "Refused by recipient", "Address not found", "Access blocked", "Parcel damaged"]
DETAIL_STOP = 2  # index of the stop the Stop detail, Proof, Failed and Reassign screens are about
NO_PHONE_STOP = DETAIL_STOP  # the stop that has no phone number, against the narration
STATUSES = ["Delivered", "Delivered", "Next", "Pending", "Pending", "Pending"]


def sample_data(seed: int = SEED) -> dict:
    """All sample values for the pages, from one seeded generator. Deterministic."""
    rng = random.Random(seed)

    def name() -> str:
        return f"{rng.choice(_FIRST_A)}{rng.choice(_FIRST_B)} {rng.choice(_SUR_A)}{rng.choice(_SUR_B)}"

    def first_of_month(lo: int = 3, hi: int = 9) -> str:
        return f"01/{rng.randint(lo, hi):02d}/2026"

    def phone() -> str:
        return f"07700 900{rng.randint(0, 999):03d}"  # the range reserved for fiction

    def parcel_id() -> str:
        return f"HL-{rng.randint(100000, 999999)}"

    driver = name()
    supervisor = name()
    other_drivers = [name() for _ in range(3)]
    route = f"R-{rng.randint(10, 99)}"
    date = first_of_month(4, 8)

    # Two of the six time windows show red. The stop the detail screens are
    # about is always one of them; the other is one of the later stops.
    late = {DETAIL_STOP, rng.choice([3, 4, 5])}

    stops = []
    used_ids: set[str] = set()
    for i in range(6):
        parcels = []
        count = 2 if i == DETAIL_STOP else rng.randint(1, 3)
        for j in range(count):
            pid = parcel_id()
            while pid in used_ids:
                pid = parcel_id()
            used_ids.add(pid)
            if i == DETAIL_STOP:
                # First parcel is over 100 pounds (photo and signature), the
                # second is small and carries the Hazardous badge.
                value = rng.choice([150, 200, 250, 300]) if j == 0 else rng.choice([20, 40, 60, 80])
                weight = rng.choice(["2.4 kg", "3.1 kg", "5.0 kg"]) if j == 0 else rng.choice(["0.4 kg", "0.6 kg", "0.8 kg"])
                hazardous = j == 1
            else:
                value = rng.choice([20, 40, 60, 80, 120, 150])
                weight = rng.choice(["0.4 kg", "0.8 kg", "1.2 kg", "2.4 kg"])
                hazardous = False
            parcels.append({"id": pid, "value": value, "weight": weight, "hazardous": hazardous})
        stops.append({
            "n": i + 1,
            "address": f"{rng.randint(1, 99)} {rng.choice(_STREETS)}",
            "recipient": name(),
            "parcels": parcels,
            "window": _WINDOWS[i],
            "late": i in late,
            "status": STATUSES[i],
            "phone": None if i == NO_PHONE_STOP else phone(),
            "instructions": rng.choice(_INSTRUCTIONS),
        })

    return {
        "driver": driver,
        "supervisor": supervisor,
        "other_drivers": [{"name": n, "left": rng.randint(2, 6)} for n in other_drivers],
        "route": route,
        "date": date,
        "town": _TOWN,
        "stops": stops,
        "previous_attempt": first_of_month(4, 8),
        "reasons": _REASONS,
        "summary": {
            "delivered": 4,
            "failed": 2,
            "pending": 0,
            "cash": rng.choice([120, 180, 240, 300, 360, 420]),
            "mileage": rng.randint(45, 95),
        },
    }


# ------------------------------------------------------------------ pages


def _esc(value) -> str:
    return html.escape(str(value))


STYLE = """\
* { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #0b1220; --card: #141d31; --card2: #1b2740; --line: #2a3752;
  --text: #eaf0fa; --muted: #98a6c0; --blue: #38a3f5; --green: #22c55e;
  --red: #ff6b6b; --amber: #fbbf24; --grey: #3a4560;
}
html, body { background: #000; }
body { font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif; color: var(--text); font-size: 15px; }
.phone { width: 390px; height: 844px; margin: 0 auto; background: var(--bg); display: flex; flex-direction: column; overflow: hidden; position: relative; }
.status { height: 44px; padding: 14px 24px 0; display: flex; justify-content: space-between; font-size: 15px; font-weight: 600; flex: none; }
.status .sys { font-weight: 500; color: var(--muted); }
.appbar { padding: 10px 20px 14px; border-bottom: 1px solid var(--line); flex: none; }
.appbar .crumb { color: var(--blue); font-size: 14px; margin-bottom: 4px; }
.appbar h1 { font-size: 26px; font-weight: 700; line-height: 1.15; }
.appbar .sub { color: var(--muted); font-size: 14px; margin-top: 4px; }
main { flex: 1; padding: 14px 16px 0; overflow: hidden; }
.home { height: 26px; flex: none; display: flex; justify-content: center; align-items: center; }
.home span { display: block; width: 130px; height: 5px; border-radius: 3px; background: #6b7891; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 12px 14px; margin-bottom: 10px; }
.card h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin-bottom: 8px; font-weight: 600; }
.strip { display: flex; gap: 8px; margin-bottom: 10px; color: var(--muted); font-size: 13px; }
.strip b { color: var(--text); }
.stop { display: flex; align-items: center; gap: 12px; background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 11px 12px; margin-bottom: 8px; }
.stop.next { border-color: var(--blue); }
.stop .num { width: 30px; height: 30px; border-radius: 15px; background: var(--card2); display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 14px; flex: none; }
.stop .body { flex: 1; min-width: 0; }
.stop .addr { font-weight: 600; font-size: 16px; }
.stop .who { color: var(--muted); font-size: 13px; margin-top: 1px; }
.win { font-size: 13px; margin-top: 3px; color: var(--text); font-variant-numeric: tabular-nums; }
.win.late { color: var(--red); font-weight: 700; }
.chip { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; padding: 4px 9px; border-radius: 999px; flex: none; }
.chip.delivered { background: #123a24; color: #6ee7a0; }
.chip.failed { background: #45161a; color: #ff9b9b; }
.chip.next { background: #0f3556; color: #7cc4ff; }
.chip.pending { background: #2a3350; color: #b4c0d8; }
.big-addr { font-size: 22px; font-weight: 700; line-height: 1.2; }
.muted { color: var(--muted); }
.row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-top: 1px solid var(--line); }
.row:first-of-type { border-top: 0; }
.row .id { font-weight: 700; font-size: 15px; font-variant-numeric: tabular-nums; }
.row .meta { color: var(--muted); font-size: 13px; margin-top: 2px; }
.badge { display: inline-block; font-size: 11px; font-weight: 800; text-transform: uppercase; letter-spacing: .05em; padding: 3px 8px; border-radius: 6px; background: #f97316; color: #1a0f00; margin-left: 8px; vertical-align: 2px; }
.btns { display: flex; gap: 10px; margin-bottom: 10px; }
.btn { flex: 1; text-align: center; padding: 14px 10px; border-radius: 12px; font-weight: 700; font-size: 16px; background: var(--blue); color: #04121f; }
.btn.off { background: var(--grey); color: #7a86a1; }
.btn.green { background: var(--green); color: #04160b; }
.btn.line { background: transparent; border: 1.5px solid var(--blue); color: var(--blue); padding: 10px; font-size: 15px; }
.btn.block { display: block; width: 100%; }
.hint { color: var(--muted); font-size: 12.5px; margin-top: 6px; text-align: center; }
.photo { border: 2px dashed var(--grey); border-radius: 14px; height: 150px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; color: var(--muted); margin-bottom: 10px; }
.photo .lens { width: 46px; height: 46px; border-radius: 23px; border: 3px solid var(--grey); }
.label { font-size: 13px; font-weight: 600; color: var(--muted); margin: 4px 0 6px; }
.req { color: var(--red); }
.input { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px; color: var(--muted); font-size: 16px; margin-bottom: 10px; }
.input.val { color: var(--text); }
.pad { border: 2px dashed var(--grey); border-radius: 14px; height: 120px; position: relative; margin-bottom: 12px; color: var(--muted); }
.pad .x { position: absolute; left: 16px; bottom: 34px; font-size: 18px; }
.pad .rule { position: absolute; left: 14px; right: 14px; bottom: 30px; border-bottom: 1px solid var(--grey); }
.pad .cap { position: absolute; left: 0; right: 0; bottom: 8px; text-align: center; font-size: 12px; }
.link { text-align: center; color: var(--red); font-weight: 600; font-size: 15px; padding: 14px 0 0; }
.pick { display: flex; align-items: center; gap: 12px; background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; margin-bottom: 8px; font-size: 16px; }
.pick .dot { width: 20px; height: 20px; border-radius: 10px; border: 2px solid var(--grey); flex: none; }
.pick.on { border-color: var(--blue); background: #0f2a45; }
.pick.on .dot { border-color: var(--blue); background: radial-gradient(var(--blue) 0 45%, transparent 50%); }
.pick .right { margin-left: auto; color: var(--muted); font-size: 13px; }
.area { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px; color: var(--muted); font-size: 15px; height: 92px; margin-bottom: 12px; line-height: 1.35; }
.area.val { color: var(--text); }
.banner { background: #3b2a06; border: 1px solid #7a5a12; color: var(--amber); border-radius: 12px; padding: 10px 14px; font-size: 14px; margin-bottom: 10px; }
.tiles { display: flex; gap: 8px; margin-bottom: 10px; }
.tile { flex: 1; background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 12px 8px; text-align: center; }
.tile .v { font-size: 30px; font-weight: 800; }
.tile .k { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin-top: 2px; }
.tile.good .v { color: #6ee7a0; }
.tile.bad .v { color: #ff9b9b; }
.kv { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-top: 1px solid var(--line); font-size: 16px; }
.kv:first-of-type { border-top: 0; }
.kv .big { font-size: 22px; font-weight: 800; }
.kv .field { background: var(--card2); border: 1px solid var(--line); border-radius: 8px; padding: 6px 12px; min-width: 96px; text-align: right; font-weight: 700; font-size: 18px; }
"""


def _window(stop: dict) -> str:
    start, end = stop["window"]
    cls = "win late" if stop["late"] else "win"
    return f'<div class="{cls}">{start} &ndash; {end}</div>'


def _chip(status: str) -> str:
    return f'<span class="chip {status.lower()}">{_esc(status)}</span>'


def _page(index: int, title: str, sub: str, body: str, data: dict, crumb: str = "") -> str:
    """Wrap a page body in the phone chrome: status bar, app bar, home indicator."""
    crumb_html = f'<div class="crumb">&lsaquo; {_esc(crumb)}</div>' if crumb else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Harbourline Drivers - {_esc(title)}</title>
<link rel="stylesheet" href="style.css"></head>
<body>
<div class="phone">
<div class="status"><span>{CLOCKS[index]}</span><span class="sys">5G &middot; 78%</span></div>
<div class="appbar">{crumb_html}<h1>{_esc(title)}</h1><div class="sub">{sub}</div></div>
<main>
{body}
</main>
<div class="home"><span></span></div>
</div>
</body></html>
"""


def page_html(index: int, data: dict) -> str:
    """The HTML of screen `index` (0..5) with the sample values filled in."""
    stops = data["stops"]
    stop = stops[DETAIL_STOP]
    addr = f"{stop['address']}, {data['town']}"
    s = data["summary"]

    if index == 0:
        cards = []
        for st in stops:
            n = len(st["parcels"])
            cards.append(
                f'<div class="stop{" next" if st["status"] == "Next" else ""}"><div class="num">{st["n"]}</div>'
                f'<div class="body"><div class="addr">{_esc(st["address"])}</div>'
                f'<div class="who">{_esc(st["recipient"])} &middot; {n} parcel{"s" if n != 1 else ""}</div>{_window(st)}</div>'
                f'{_chip(st["status"])}</div>'
            )
        body = (f'<div class="strip"><span><b>6</b> stops</span><span>&middot;</span><span><b>2</b> delivered</span>'
                f'<span>&middot;</span><span><b>1</b> next</span></div>\n' + "\n".join(cards))
        return _page(0, "Today's runs", f"{_esc(data['driver'])} &middot; Route {_esc(data['route'])} &middot; {_esc(data['date'])}", body, data)

    if index == 1:
        rows = "".join(
            f'<div class="row"><div><div class="id">{_esc(p["id"])}{"<span class=badge>Hazardous</span>" if p["hazardous"] else ""}</div>'
            f'<div class="meta">{_esc(p["weight"])}</div></div><div class="id">&pound;{p["value"]}</div></div>'
            for p in stop["parcels"]
        )
        phone = ('<div class="muted">No number on file</div>' if stop["phone"] is None
                 else f'<div>{_esc(stop["phone"])}</div>')
        body = f"""<div class="card"><div class="big-addr">{_esc(addr)}</div>
<div class="muted" style="margin-top:4px">{_esc(stop['recipient'])}</div>{_window(stop)}</div>
<div class="card"><h2>{len(stop['parcels'])} parcels</h2>{rows}</div>
<div class="card"><h2>Instructions</h2><div>{_esc(stop['instructions'])}</div></div>
<div class="card"><h2>Phone</h2>{phone}</div>
<div class="btns"><div class="btn off">Call</div><div class="btn">Navigate</div></div>
<div class="btn block">Proof of delivery</div>"""
        return _page(1, "Stop detail", f"Stop {stop['n']} of 6 &middot; {_chip(stop['status'])}", body, data, crumb="Today's runs")

    if index == 2:
        first = stop["parcels"][0]
        body = f"""<div class="photo"><div class="lens"></div><div>No photo yet</div></div>
<div class="btn line block" style="margin-bottom:12px">Take photo</div>
<div class="label">Recipient name <span class="req">*</span></div>
<div class="input">Who took the parcel?</div>
<div class="label">Signature</div>
<div class="pad"><span class="x">x</span><span class="rule"></span><span class="cap">Sign here with a finger</span></div>
<div class="btn off block">Delivered</div>
<div class="hint">Take a photo to continue</div>
<div class="link">Can't deliver? Report a failed delivery</div>"""
        return _page(2, "Proof of delivery", f"Stop {stop['n']} &middot; {_esc(first['id'])} and 1 more", body, data, crumb="Stop detail")

    if index == 3:
        picks = "".join(
            f'<div class="pick{" on" if i == 0 else ""}"><span class="dot"></span>{_esc(r)}</div>'
            for i, r in enumerate(data["reasons"])
        )
        body = f"""<div class="label">Reason <span class="req">*</span></div>
{picks}
<div class="label" style="margin-top:8px">Anything else the depot should know?</div>
<div class="area">Type here</div>
<div class="btn block" style="background:var(--red);color:#1f0505">Mark as failed</div>
<div class="hint">Last attempt: {_esc(data['previous_attempt'])} &middot; Nobody home</div>"""
        return _page(3, "Failed delivery", f"Stop {stop['n']} &middot; Attempt 2 of 3", body, data, crumb="Proof of delivery")

    if index == 4:
        picks = "".join(
            f'<div class="pick{" on" if i == 0 else ""}"><span class="dot"></span>{_esc(d["name"])}<span class="right">{d["left"]} stops left</span></div>'
            for i, d in enumerate(data["other_drivers"])
        )
        body = f"""<div class="banner">Supervisor only. Signed in as {_esc(data['supervisor'])} (Supervisor).</div>
<div class="card"><h2>Stop {stop['n']}</h2><div style="font-weight:700;font-size:17px">{_esc(addr)}</div>
<div class="muted" style="margin-top:3px">Currently with {_esc(data['driver'])}</div></div>
<div class="label">New driver <span class="req">*</span></div>
{picks}
<div class="label" style="margin-top:8px">Reason for reassigning <span class="req">*</span></div>
<div class="area val">Van off the road, moving the rest of the route.</div>
<div class="btn block">Reassign stop</div>"""
        return _page(4, "Reassign stop", f"Stop {stop['n']} &middot; {_esc(data['route'])}", body, data, crumb="Stop detail")

    body = f"""<div class="tiles">
<div class="tile good"><div class="v">{s['delivered']}</div><div class="k">Delivered</div></div>
<div class="tile bad"><div class="v">{s['failed']}</div><div class="k">Failed</div></div>
<div class="tile"><div class="v">{s['pending']}</div><div class="k">Pending</div></div>
</div>
<div class="card">
<div class="kv"><span>Stops</span><span class="big">6</span></div>
<div class="kv"><span>Cash collected</span><span class="big">&pound;{s['cash']:.2f}</span></div>
<div class="kv"><span>Mileage</span><span class="field">{s['mileage']} mi</span></div>
</div>
<div class="btn green block" style="margin-top:14px">Submit day</div>
<div class="hint">Route {_esc(data['route'])} &middot; {_esc(data['date'])}</div>"""
    return _page(5, "End of day summary", f"{_esc(data['driver'])} &middot; Route {_esc(data['route'])}", body, data)


def write_pages(data: dict) -> list[Path]:
    """Write app/style.css and app/<file>.html for every screen."""
    APP.mkdir(exist_ok=True)
    (APP / "style.css").write_text(STYLE, encoding="utf-8")
    written = []
    for index, (_, file, _) in enumerate(SCRIPT):
        path = APP / file
        path.write_text(page_html(index, data), encoding="utf-8")
        written.append(path)
    return written


# ------------------------------------------------------------------ rendering


def render_with_playwright(out: Path) -> None:
    """Screenshot every page in a mobile-emulated Chromium and resize to the video size."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": CSS_W, "height": CSS_H},
            device_scale_factor=SCALE,
            is_mobile=True,
            has_touch=True,
        )
        page = context.new_page()
        for index, (_, file, _) in enumerate(SCRIPT):
            page.goto((APP / file).resolve().as_uri())
            page.wait_for_load_state("load")
            raw = out / f"page_{index:02d}_raw.png"
            page.screenshot(path=str(raw))
            with Image.open(raw) as img:
                # 780x1688 -> 540x1170: the aspect differs by about 0.1%, which is invisible.
                img.convert("RGB").resize((VIDEO_W, VIDEO_H), Image.LANCZOS).save(out / f"page_{index:02d}.png")
        browser.close()


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

    data = sample_data()
    write_pages(data)
    print(f"pages: {len(SCRIPT)} written to {APP} from seed {SEED}")

    try:
        render_with_playwright(WORK)
    except ImportError:
        print("render: playwright is not installed (.venv/bin/pip install playwright && .venv/bin/playwright install chromium)")
        return 1
    print(f"render: {len(SCRIPT)} screens drawn at {CSS_W}x{CSS_H} CSS px, x{SCALE}, resized to {VIDEO_W}x{VIDEO_H} -> {WORK}")

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
        print(f"  {start:6.2f} - {end:6.2f}  screen_{i:02d}  {screen}  (clock {CLOCKS[i]})")
        for c_start, c_end, c_screen, text in cues:
            if c_screen == screen:
                print(f"           {c_start:6.2f} - {c_end:6.2f}    {text}")
    print(f"  total {total:.2f}s, {len(cues)} lines\n")

    # One still per screen, kept in frames/ so the recording can be looked at without a player.
    for i in range(len(SCRIPT)):
        shutil.copyfile(WORK / f"page_{i:02d}.png", FRAMES / f"screen_{i:02d}.png")

    # Video: each screen's still shown for the screen's span, then muxed with the audio.
    concat = WORK / "screens.txt"
    lines_out = []
    last = None
    for i, (_, start, end) in enumerate(screen_spans):
        last = (FRAMES / f"screen_{i:02d}.png").resolve()
        lines_out.append(f"file '{last}'")
        lines_out.append(f"duration {end - start:.3f}")
    lines_out.append(f"file '{last}'")  # concat quirk: last entry needs repeating
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
    print(f"wrote {MP4.name} ({size_mb:.2f} MB, {codec}, {total:.1f}s, {VIDEO_W}x{VIDEO_H}) and {VTT.name} ({len(cues)} cues), voice {VOICE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
