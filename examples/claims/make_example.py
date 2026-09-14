"""Build the claims example recording: walkthrough.mp4 and walkthrough.vtt.

A second standing example, deliberately unlike the onboarding one: a fake
complaints-handling tool ("Meridian Complaints") whose screens are mostly
tables, timelines and a chart rather than forms, recorded inside a Teams-style
meeting frame instead of full-screen. The shared window is the 1280x720 area at
(160, 60) of a 1600x900 canvas, so `specto run ... --crop auto` has a border,
a toolbar and a gallery strip to find and drop.

What it does, in order:

1. Generates every sample value (customer names, references, dates, amounts,
   phone numbers) from one seeded random generator, so the pages are the same
   on every rebuild and contain no real person or firm.
2. Writes the six pages of the fake app into `app/` as plain HTML, then
   renders each to a 1280x720 PNG with Playwright (headless Chromium), or
   draws a plain stand-in with Pillow if the browser is missing.
3. Pastes each page into the meeting frame twice: once with the "mic" dot on
   one participant tile lit, once with it dark, so the dot blinks in the video.
   The meeting timer in the top bar shows the time each screen came up.
4. Speaks each narration line with the Mac's `say` command (Daniel), measures
   the clips with ffmpeg, lays them out screen by screen with a short pause
   after each line, joins the audio, builds the video from the frames, muxes
   the two, and writes a WebVTT file from the same timeline.

Run it from the repo root with the project venv:

    .venv/bin/python examples/claims/make_example.py

Outputs land next to this file. Nothing here touches the specto package.

Why the timer and the mic dot are small: the share-region detector counts a
row or column as part of the shared window only when at least 2% of its
pixels change over the recording (STRIP_FRACTION in specto/ingest.py). On a
1600x900 canvas that is 32 pixels along a row and 18 down a column, so the
timer is drawn in an 11 px font (at most four digits, about 24 px, ever
differ) and the mic dot is 10 px across. Both change, neither is big enough
to pull the box off the window.
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
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
APP = HERE / "app"
WORK = HERE / "build"
FRAMES = HERE / "frames"
MP4 = HERE / "walkthrough.mp4"
VTT = HERE / "walkthrough.vtt"

WIDTH, HEIGHT = 1280, 720  # the shared window (one app page)
CANVAS_W, CANVAS_H = 1600, 900  # the meeting recording
WINDOW_X, WINDOW_Y = 160, 60  # where the shared window sits in the canvas
PAUSE = 0.6  # seconds of silence after every line
LEAD_IN = 0.8  # silence before the first line
BLINK = 1.0  # seconds the mic dot stays on (and then off)
VOICE = "Daniel"
SAMPLE_RATE = 22050
SEED = 20260914

# One accent colour per screen; the app paints it on the stripes at the top and
# bottom of the window and on the active nav item, so every page differs from
# the last right up to the window's edges. The share-region detector compares
# grey levels (a change counts above 24 of 255), so the colours are chosen to
# differ in brightness from the first screen's, not only in hue: navy first
# (grey 59), then teal 104, magenta 103, amber 136, red 92, green 111.
ACCENTS = ["#1e3a8a", "#0d9488", "#c026d3", "#d97706", "#e11d48", "#16a34a"]

# (screen name, html file, [narration lines]). The screen shown on video is the
# one whose lines are being spoken. Each line is what an expert would say.
SCRIPT: list[tuple[str, str, list[str]]] = [
    ("Complaints Inbox", "inbox.html", [
        "Right, this is Meridian Complaints, the tool we log and work every customer complaint in. You land on the Inbox, which is every open complaint sorted by how long we've got left on it.",
        "Each row has the reference, the customer, when it came in, the category, the status and the SLA countdown. You click a column header to sort, and most people sort by the SLA column.",
        "Anything over eight weeks old goes to the Ombudsman automatically, you'll see the status flip to Referred on its own. Sometimes the SLA clock is wrong, honestly, nobody knows why, so you double-check the received date.",
    ]),
    ("Complaint Detail", "detail.html", [
        "You double-click a row to open it and you get the Complaint Detail. Top left is the customer and their contact details, and on the right is the timeline, every event in order: received, acknowledged, assigned, and so on.",
        "The notes panel at the bottom is where you write what you've done. You type in the box and press Add note, and it stamps your name and the date on it. Notes can't be edited or deleted once they're saved.",
        "The category is set by whoever logged the complaint. If it's wrong you can change it here, but you have to put a note in saying why.",
    ]),
    ("Decision", "decision.html", [
        "When you've investigated, you go to Decision. It's three options: uphold, partially uphold, or reject. You pick one, choose a reason from the dropdown, and write the summary in the box.",
        "The summary is mandatory and it has to be at least fifty words, the system counts them. Then you press Record decision.",
        "Only complaint handlers at grade two and above can record a decision. If you're a trainee the button is greyed out and your supervisor records it for you.",
    ]),
    ("Redress", "redress.html", [
        "If it's upheld or partially upheld, you go to Redress. You put in the refund amount and the number of days the customer was out of pocket, and it works out the interest and the total in the little table.",
        "The interest rate comes from somewhere in finance, I just use whatever it shows. Then you press Approve redress. Anything over five hundred pounds needs a second approver, and that pops up as a task for the team lead.",
    ]),
    ("Letter Preview", "letter.html", [
        "Letter Preview is the final response letter. It's a template with the customer's name, the reference, the decision and the redress amount merged in from the earlier screens.",
        "You can't send a letter until a decision is recorded, the Send letter button just won't show up. You read it through, and if it's right you press Send letter and it goes to print.",
        "If the customer asked for email, it's supposed to email it instead of printing. I'm not sure where that preference is set, it's not on any of these screens.",
    ]),
    ("Dashboard", "dashboard.html", [
        "And the Dashboard is what the team lead looks at. Three numbers: open complaints, how many are breaching SLA, and the uphold rate, and a chart of complaints received by month.",
        "I think the uphold rate is this month, but it might be rolling, I've never checked. You press Export to get it all as a spreadsheet. That's the whole process, inbox to letter.",
    ]),
]

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace")


# ------------------------------------------------------------------ sample data
#
# Every value on the pages comes out of `sample_data`, seeded with SEED, so the
# example is the same on every rebuild. Names are built from syllables, so they
# are not anyone's; firms, addresses and policies are invented the same way.

_FIRST_A = ["Ma", "Lo", "Ri", "Ta", "Ne", "Sa", "Vi", "Ka", "Jo", "El", "Da", "Fe"]
_FIRST_B = ["ra", "nel", "sha", "na", "vin", "lo", "mi", "dor", "ta", "ren", "lia", "bo"]
_SUR_A = ["Har", "Bel", "Cor", "Dun", "Fal", "Gra", "Mor", "Pen", "Ros", "Wal", "Ash", "Kel"]
_SUR_B = ["mond", "ley", "wick", "ford", "ston", "brook", "dale", "ridge", "ton", "by", "combe", "well"]
_CATEGORIES = ["Delayed claim", "Declined claim", "Premium increase", "Poor service", "Policy wording", "Renewal", "Mis-sold cover"]
_STATUSES = ["New", "Investigating", "Awaiting customer", "Decided", "Referred"]
_TOWNS = ["Ashby Vale", "Corwick", "Penbrook", "Morston", "Falridge", "Dunley"]
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def sample_data(seed: int = SEED) -> dict:
    """All sample values for the pages, from one seeded generator. Deterministic."""
    rng = random.Random(seed)

    def name() -> str:
        return f"{rng.choice(_FIRST_A)}{rng.choice(_FIRST_B)} {rng.choice(_SUR_A)}{rng.choice(_SUR_B)}"

    def reference() -> str:
        return f"CMP-2026-0{rng.randint(1000, 9999)}"  # CMP-2026-0xxxx

    def first_of_month(lo: int = 3, hi: int = 9) -> str:
        return f"01/{rng.randint(lo, hi):02d}/2026"

    def phone() -> str:
        return f"07700 900{rng.randint(0, 999):03d}"  # the range reserved for fiction

    def money(choices=(50, 100, 150, 200, 250, 300, 400, 500, 750, 1000)) -> int:
        return rng.choice(choices)

    handler = name()
    team_lead = name()
    logger = name()

    # Eight inbox rows, then sorted by SLA hours left, fewest first. The row at
    # the top is the one the rest of the walkthrough opens.
    refs: set[str] = set()
    rows = []
    for i in range(8):
        ref = reference()
        while ref in refs:
            ref = reference()
        refs.add(ref)
        status = _STATUSES[i % len(_STATUSES)]
        hours = -rng.randint(24, 200) if status == "Referred" else rng.randint(6, 55 * 24)
        rows.append({
            "ref": ref,
            "customer": name(),
            "received": first_of_month(4, 8),
            "category": rng.choice(_CATEGORIES),
            "status": status,
            "owner": rng.choice([handler, team_lead, logger, "Unassigned"]),
            "sla_hours": hours,
        })
    rows.sort(key=lambda r: (r["sla_hours"] < 0, r["sla_hours"]))
    open_row = next(r for r in rows if r["status"] in ("Investigating", "New"))
    rows.remove(open_row)
    rows.insert(0, open_row)

    first, last = open_row["customer"].split(" ")
    amount = money()
    rate = 8.0  # the fixed rate the pages show as "from the Finance rate table"
    days = rng.choice([30, 60, 90, 120])
    interest = round(amount * rate / 100 * days / 365, 2)
    return {
        "handler": handler,
        "team_lead": team_lead,
        "logger": logger,
        "rows": rows,
        "complaint": {
            **open_row,
            "email": f"{first.lower()}.{last.lower()}@example.com",
            "phone": phone(),
            "policy": f"POL-{rng.randint(100000, 999999)}",
            "address": f"{rng.randint(1, 99)} {rng.choice(_SUR_A)}{rng.choice(_SUR_B)} Road, {rng.choice(_TOWNS)}",
            "product": rng.choice(["Home insurance", "Motor insurance", "Travel insurance", "Pet insurance"]),
            "channel": rng.choice(["Phone", "Email", "Web form", "Letter"]),
        },
        "timeline": [
            (open_row["received"], "Complaint received", f"Logged by {logger} from a {rng.choice(['phone call', 'web form', 'letter'])}"),
            (open_row["received"], "Acknowledgement sent", "Standard acknowledgement letter, 4 working days"),
            (first_of_month(6, 7), f"Assigned to {handler}", "Auto-assigned from the Delayed claim queue"),
            (first_of_month(7, 8), "Customer contacted", "Called the customer, left a message"),
            (first_of_month(7, 8), "Evidence requested", "Asked claims for the claim file and call recordings"),
            (first_of_month(8, 9), "Final response due", "8-week deadline"),
        ],
        "notes": [
            (handler, first_of_month(6, 7), "Read the claim file. The claim sat unallocated for three weeks before anyone picked it up."),
            (handler, first_of_month(7, 8), "Customer says they were told the payment would be with them in five days. Requested the call recording."),
        ],
        "decision": {
            "outcome": "Partially uphold",
            "reason": "Delay not fully explained",
            "summary": "The claim was accepted and paid in full, but it took longer than it should have. The customer was given a timescale that was not met.",
        },
        "redress": {"amount": amount, "rate": rate, "days": days, "interest": interest, "total": round(amount + interest, 2)},
        "dashboard": {
            "open": rng.randint(30, 60),
            "breaching": rng.randint(2, 9),
            "uphold_rate": rng.randint(35, 65),
            "by_month": [(_MONTHS[m - 1][:3], rng.randint(10, 40)) for m in range(4, 10)],
        },
    }


# ------------------------------------------------------------------ pages


def _esc(value) -> str:
    return html.escape(str(value))


def _sla(hours: int) -> str:
    if hours < 0:
        return '<span class="sla breach">Breached</span>'
    days, rem = divmod(hours, 24)
    cls = "breach" if hours < 48 else "warn" if hours < 24 * 7 else "ok"
    return f'<span class="sla {cls}">{days}d {rem:02d}h</span>'


def _pill(status: str) -> str:
    cls = status.split(" ")[0].lower()
    return f'<span class="pill {cls}">{_esc(status)}</span>'


def _page(index: int, title: str, subtitle: str, body: str, data: dict, thumb: tuple[int, int]) -> str:
    """Wrap a page body in the app chrome: stripes, header, nav, scrollbar, footer."""
    nav = []
    for i, (name, file, _) in enumerate(SCRIPT):
        cls = "active" if i == index else ""
        nav.append(f'<a class="{cls}" href="{file}">{_esc(name)}</a>')
    top, height = thumb
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Meridian Complaints - {_esc(title)}</title>
<link rel="stylesheet" href="style.css"></head>
<body style="--accent: {ACCENTS[index]}">
<div class="stripe"></div>
<header><div class="brand"><span></span>Meridian Complaints</div><div class="where">&rsaquo; {_esc(title)}</div>
<div class="user">Signed in as <b>{_esc(data['handler'])}</b> &middot; Complaint Handler, grade 2</div></header>
<div class="layout">
<nav><div class="section">Complaints</div>
{chr(10).join(nav)}
<div class="section" style="margin-top:14px">Other</div><a href="#">Reports</a><a href="#">Settings</a></nav>
<main>
<h1>{_esc(title)}</h1>
<p class="subtitle">{subtitle}</p>
{body}
<div class="scrollbar"><div class="thumb" style="top:{top}px;height:{height}px"></div></div>
</main>
</div>
<footer>Meridian Complaints v2.7 &middot; {_esc(title)} &middot; Internal use only &middot; Help: ext. 2210</footer>
<div class="stripe"></div>
</body></html>
"""


def page_html(index: int, data: dict) -> str:
    """The HTML of screen `index` (0..5) with the sample values filled in."""
    c = data["complaint"]
    if index == 0:
        rows = []
        for r in data["rows"]:
            sel = ' class="selected"' if r["ref"] == c["ref"] else ""
            rows.append(
                f"<tr{sel}><td>{_esc(r['ref'])}</td><td>{_esc(r['customer'])}</td><td>{_esc(r['received'])}</td>"
                f"<td>{_esc(r['category'])}</td><td>{_pill(r['status'])}</td><td>{_esc(r['owner'])}</td><td>{_sla(r['sla_hours'])}</td>"
                f'<td><a class="btn small" href="detail.html">Open</a></td></tr>'
            )
        body = f"""<div class="card">
<table>
<tr><th>Reference<span class="sort">&#9662;</span></th><th>Customer<span class="sort">&#9662;</span></th><th>Received<span class="sort">&#9662;</span></th>
<th>Category<span class="sort">&#9662;</span></th><th>Status<span class="sort">&#9662;</span></th><th>Owner<span class="sort">&#9662;</span></th>
<th class="sorted">SLA remaining<span class="sort">&#9652;</span></th><th></th></tr>
{chr(10).join(rows)}
</table></div>
<p class="muted">Showing {len(data['rows'])} open complaints &middot; Final response due within 8 weeks of receipt &middot; Double-click a row to open it</p>"""
        return _page(0, "Complaints Inbox", "Open complaints assigned to your team, sorted by SLA remaining.", body, data, (6, 200))

    if index == 1:
        events = "".join(
            f'<li><div class="when">{_esc(when)}</div><div class="what">{_esc(what)}</div><div class="muted">{_esc(who)}</div></li>'
            for when, what, who in data["timeline"]
        )
        notes = "".join(
            f'<div class="note"><div class="meta">{_esc(who)} &middot; {_esc(when)}</div>{_esc(text)}</div>'
            for who, when, text in data["notes"]
        )
        body = f"""<div class="cols">
<div class="card narrow"><h2>Customer</h2>
<table class="kv">
<tr><td>Name</td><td>{_esc(c['customer'])}</td></tr>
<tr><td>Policy</td><td>{_esc(c['policy'])} &middot; {_esc(c['product'])}</td></tr>
<tr><td>Phone</td><td>{_esc(c['phone'])}</td></tr>
<tr><td>Email</td><td>{_esc(c['email'])}</td></tr>
<tr><td>Address</td><td>{_esc(c['address'])}</td></tr>
<tr><td>Received</td><td>{_esc(c['received'])} by {_esc(c['channel'])}</td></tr>
<tr><td>Category</td><td>{_esc(c['category'])} <a class="btn small" href="#">Change</a></td></tr>
<tr><td>Status</td><td>{_pill(c['status'])}</td></tr>
<tr><td>Owner</td><td>{_esc(c['owner'])}</td></tr>
</table></div>
<div class="card"><h2>Timeline</h2><ul class="timeline">{events}</ul></div>
</div>
<div class="card"><h2>Notes</h2>{notes}
<textarea placeholder="Write a note. Notes are stamped with your name and today's date and cannot be edited afterwards."></textarea>
<div class="actions"><a class="btn primary" href="#">Add note</a><span class="muted">Notes are permanent once saved.</span></div></div>"""
        return _page(1, f"Complaint {c['ref']}", f"{_esc(c['customer'])} &middot; {_esc(c['category'])} &middot; SLA {_sla(c['sla_hours'])}", body, data, (60, 120))

    if index == 2:
        d = data["decision"]
        opts = [("Uphold", "The complaint is justified in full. Redress is usually due."),
                ("Partially uphold", "Part of the complaint is justified. Redress may be due."),
                ("Reject", "The complaint is not justified. No redress.")]
        radios = "".join(
            f'<label class="{"on" if o == d["outcome"] else ""}"><input type="radio" name="outcome" {"checked" if o == d["outcome"] else ""}><div><b>{o}</b><span>{why}</span></div></label>'
            for o, why in opts
        )
        body = f"""<div class="notice"><b>Grade 2 and above.</b> Only complaint handlers at grade 2 or above can record a decision. Trainees: ask your supervisor to record it.</div>
<div class="cols">
<div class="card narrow"><h2>Outcome <span class="req">*</span></h2><div class="radios">{radios}</div></div>
<div class="card"><h2>Reason and summary</h2>
<div class="field"><label>Reason<span class="req">*</span></label><select><option>{_esc(d['reason'])}</option><option>Claim wrongly declined</option><option>Service below standard</option><option>Terms clearly explained</option></select></div>
<div class="field" style="margin-top:10px"><label>Summary of findings<span class="req">*</span></label>
<textarea style="height:110px">{_esc(d['summary'])}</textarea>
<div class="hint">At least 50 words. Currently 31 words.</div></div>
<div class="actions"><a class="btn primary" href="#">Record decision</a><a class="btn" href="#">Save draft</a></div>
</div></div>"""
        return _page(2, "Decision", f"Complaint {_esc(c['ref'])} &middot; {_esc(c['customer'])}", body, data, (140, 140))

    if index == 3:
        r = data["redress"]
        body = f"""<div class="info">Interest is simple interest at the Finance rate table figure, calculated from the number of days the customer was out of pocket.</div>
<div class="cols">
<div class="card narrow"><h2>Inputs</h2>
<div class="field"><label>Refund amount (&pound;)<span class="req">*</span></label><input type="text" value="{r['amount']:.2f}"></div>
<div class="field" style="margin-top:10px"><label>Days out of pocket<span class="req">*</span></label><input type="text" value="{r['days']}"></div>
<div class="field" style="margin-top:10px"><label>Interest rate</label><input type="text" readonly value="{r['rate']:.2f}% per year"><div class="hint">From the Finance rate table. Not editable here.</div></div>
</div>
<div class="card"><h2>Calculation</h2>
<table>
<tr><th>Item</th><th class="num">Amount</th></tr>
<tr><td>Refund</td><td class="num">&pound;{r['amount']:,.2f}</td></tr>
<tr><td>Interest ({r['rate']:.0f}% for {r['days']} days)</td><td class="num">&pound;{r['interest']:,.2f}</td></tr>
<tr><td><b>Total redress</b></td><td class="num"><b>&pound;{r['total']:,.2f}</b></td></tr>
</table>
<div class="actions"><a class="btn primary" href="#">Approve redress</a><a class="btn" href="#">Recalculate</a><span class="muted">Redress over &pound;500 needs a second approver (team lead).</span></div>
</div></div>"""
        return _page(3, "Redress", f"Complaint {_esc(c['ref'])} &middot; {_esc(c['customer'])} &middot; {_esc(data['decision']['outcome'])}", body, data, (220, 160))

    if index == 4:
        r = data["redress"]

        def m(field: str, value) -> str:
            return f'<span class="merge" title="{{{{{field}}}}}">{_esc(value)}</span>'

        body = f"""<div class="info">Merge fields are highlighted. The letter is sent by post unless the customer asked for email.</div>
<div class="letter">
<p>{m('today', '01/09/2026')}</p>
<p>{m('customer_name', c['customer'])}<br>{m('customer_address', c['address'])}</p>
<p>Our reference: {m('reference', c['ref'])}<br>Policy: {m('policy', c['policy'])}</p>
<p>Dear {m('salutation', c['customer'].split(' ')[0])},</p>
<p>Thank you for your complaint about your {m('product', c['product'].lower())}, which we received on {m('received', c['received'])}. We have now finished looking into it and this letter is our final response.</p>
<p>We have decided to {m('decision', data['decision']['outcome'].lower())} your complaint. {m('summary', data['decision']['summary'])}</p>
<p>We will pay you {m('redress_total', f"£{r['total']:,.2f}")}, made up of a refund of {m('redress_amount', f"£{r['amount']:,.2f}")} and interest of {m('redress_interest', f"£{r['interest']:,.2f}")}, within 10 working days.</p>
<p>If you are not happy with this response you can refer your complaint to the Ombudsman within six months of the date of this letter.</p>
<p>Yours sincerely,<br>{m('handler', data['handler'])}<br>Complaints Team, Meridian</p>
</div>
<div class="actions"><a class="btn primary" href="#">Send letter</a><a class="btn" href="#">Edit template</a><span class="muted">Send letter appears only once a decision is recorded.</span></div>"""
        return _page(4, "Letter Preview", f"Final response for {_esc(c['ref'])} &middot; {_esc(c['customer'])}", body, data, (300, 100))

    d = data["dashboard"]
    peak = max(n for _, n in d["by_month"])
    bars = "".join(
        f'<div class="bar" style="height:{int(180 * n / peak)}px"><div class="n">{n}</div><div class="w">{label} 2026</div></div>'
        for label, n in d["by_month"]
    )
    body = f"""<div class="tiles">
<div class="tile"><div class="label">Open complaints</div><div class="value">{d['open']}</div><div class="delta">across the team today</div></div>
<div class="tile bad"><div class="label">Breaching SLA</div><div class="value">{d['breaching']}</div><div class="delta">past the 8-week deadline</div></div>
<div class="tile"><div class="label">Uphold rate</div><div class="value">{d['uphold_rate']}%</div><div class="delta">upheld or partially upheld</div></div>
</div>
<div class="card"><h2>Complaints received by month</h2><div class="chart">{bars}</div>
<div class="actions"><a class="btn primary" href="#">Export</a><span class="muted">Exports the figures above as a spreadsheet.</span></div></div>"""
    return _page(5, "Dashboard", f"Team view for {_esc(data['team_lead'])}'s team.", body, data, (380, 260))


def write_pages(data: dict) -> list[Path]:
    """Write app/<file>.html for every screen. style.css is kept by hand."""
    APP.mkdir(exist_ok=True)
    written = []
    for index, (_, file, _) in enumerate(SCRIPT):
        path = APP / file
        path.write_text(page_html(index, data), encoding="utf-8")
        written.append(path)
    return written


# ------------------------------------------------------------------ rendering


def render_with_playwright(out: Path) -> bool:
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
            for index, (_, file, _) in enumerate(SCRIPT):
                page.goto((APP / file).resolve().as_uri())
                page.wait_for_load_state("load")
                page.screenshot(path=str(out / f"page_{index:02d}.png"))
            browser.close()
    except Exception as exc:  # browser missing, sandbox, etc.
        print(f"render: playwright could not run ({type(exc).__name__}: {exc})")
        return False
    return True


def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    for name in (["Helvetica-Bold", "Arial Bold"] if bold else ["Helvetica", "Arial"]):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def render_with_pillow(data: dict, out: Path) -> None:
    """Draw a plain stand-in for each screen without a browser: chrome, title, a sketched table."""
    for index, (name, _, _) in enumerate(SCRIPT):
        accent = tuple(int(ACCENTS[index][i:i + 2], 16) for i in (1, 3, 5))
        img = Image.new("RGB", (WIDTH, HEIGHT), (238, 241, 244))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, WIDTH, 4], fill=accent)
        d.rectangle([0, 4, WIDTH, 52], fill=(18, 52, 59))
        d.text((20, 18), "Meridian Complaints  >  " + name, fill="white", font=font(16, True))
        d.rectangle([0, 52, 200, HEIGHT - 30], fill="white", outline=(217, 222, 228))
        for i, (nav_name, _, _) in enumerate(SCRIPT):
            y = 72 + i * 34
            if i == index:
                d.rectangle([0, y - 6, 200, y + 22], fill=(230, 242, 241))
                d.rectangle([0, y - 6, 3, y + 22], fill=accent)
            d.text((22, y), nav_name, fill=(18, 52, 59), font=font(13, i == index))
        d.text((224, 70), name, fill=(28, 43, 57), font=font(22, True))
        d.rectangle([224, 110, 1250, 110 + 40 * 9], fill="white", outline=(217, 222, 228))
        for row in range(9):
            y = 110 + 40 * row
            d.line([234, y + 40, 1240, y + 40], fill=(228, 231, 235))
            if row:
                d.text((244, y + 12), f"{data['rows'][row - 1]['ref']}   {data['rows'][row - 1]['customer']}", fill=(28, 43, 57), font=font(13))
        d.rectangle([1268, 60 + index * 60, 1278, 200 + index * 60], fill=(154, 167, 179))
        d.rectangle([0, HEIGHT - 30, WIDTH, HEIGHT - 4], fill="white", outline=(228, 231, 235))
        d.text((24, HEIGHT - 24), f"Meridian Complaints v2.7 - {name}", fill=(138, 150, 163), font=font(11))
        d.rectangle([0, HEIGHT - 4, WIDTH, HEIGHT], fill=accent)
        img.save(out / f"page_{index:02d}.png")


# ------------------------------------------------------------------ meeting frame

FRAME_BG = (32, 33, 36)
BAR_BG = (41, 42, 45)
TILES = [("MH", (84, 110, 122)), ("RD", (121, 85, 72)), ("AK", (69, 90, 100)), ("SW", (96, 125, 139))]
MIC_TILE = 1  # which participant tile carries the blinking mic dot
MIC_DOT = 10  # px across: below the detector's 2% of a row (32 px) and of a column (18 px)
TIMER_FONT = 11  # px: four changed digits are about 24 px wide, below 32


def compose_meeting_frame(page: Image.Image, timer: str, mic_on: bool) -> Image.Image:
    """Paste a 1280x720 page into a 1600x900 Teams-style call: dark border, top bar
    with the meeting name and `timer`, four participant tiles down the right, a
    toolbar along the bottom. The page lands at (WINDOW_X, WINDOW_Y)."""
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), FRAME_BG)
    d = ImageDraw.Draw(canvas)
    # Top bar: meeting name, a "sharing" badge, and the timer.
    d.rectangle([0, 0, CANVAS_W, 44], fill=BAR_BG)
    d.text((20, 14), "Complaints handling walkthrough  -  Meridian", fill=(230, 230, 230), font=font(14, True))
    d.rounded_rectangle([WINDOW_X, 47, WINDOW_X + 168, 57], radius=4, fill=(60, 120, 80))
    d.text((WINDOW_X + 6, 47), "You are sharing a window", fill=(240, 240, 240), font=font(8))
    d.text((CANVAS_W - 62, 16), timer, fill=(200, 200, 200), font=font(TIMER_FONT))
    # The shared window.
    canvas.paste(page.convert("RGB").resize((WIDTH, HEIGHT)), (WINDOW_X, WINDOW_Y))
    d.rectangle([WINDOW_X - 1, WINDOW_Y - 1, WINDOW_X + WIDTH, WINDOW_Y + HEIGHT], outline=(70, 72, 76))
    # Participant tiles down the right-hand strip.
    tile_w, tile_h, gap = 128, 96, 12
    x = WINDOW_X + WIDTH + 16
    for i, (initials, colour) in enumerate(TILES):
        y = WINDOW_Y + i * (tile_h + gap)
        d.rounded_rectangle([x, y, x + tile_w, y + tile_h], radius=6, fill=colour)
        d.ellipse([x + 44, y + 22, x + 84, y + 62], fill=(240, 240, 240))
        f = font(15, True)
        tw = d.textlength(initials, font=f)
        d.text((x + 64 - tw / 2, y + 33), initials, fill=colour, font=f)
        d.text((x + 8, y + tile_h - 18), ["Mira H.", "Rob D.", "Ana K.", "Sam W."][i], fill=(240, 240, 240), font=font(10))
        if i == MIC_TILE and mic_on:
            mx, my = x + tile_w - 18, y + tile_h - 18
            d.ellipse([mx, my, mx + MIC_DOT, my + MIC_DOT], fill=(74, 222, 128))
    # Bottom toolbar with buttons.
    d.rectangle([0, CANVAS_H - 80, CANVAS_W, CANVAS_H], fill=BAR_BG)
    labels = [("Mic", (66, 68, 72)), ("Camera", (66, 68, 72)), ("Share", (60, 120, 80)), ("Chat", (66, 68, 72)), ("People", (66, 68, 72)), ("Leave", (196, 56, 56))]
    bx = CANVAS_W // 2 - (len(labels) * 96) // 2
    for i, (label, colour) in enumerate(labels):
        x0 = bx + i * 96
        d.rounded_rectangle([x0, CANVAS_H - 62, x0 + 84, CANVAS_H - 20], radius=8, fill=colour)
        f = font(12, True)
        tw = d.textlength(label, font=f)
        d.text((x0 + 42 - tw / 2, CANVAS_H - 48), label, fill=(240, 240, 240), font=f)
    return canvas


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


def timer_text(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


def main() -> int:
    WORK.mkdir(exist_ok=True)
    FRAMES.mkdir(exist_ok=True)

    data = sample_data()
    write_pages(data)
    print(f"pages: {len(SCRIPT)} written to {APP} from seed {SEED}")

    if render_with_playwright(WORK):
        renderer = "playwright"
    else:
        render_with_pillow(data, WORK)
        renderer = "pillow"
    print(f"render: {len(SCRIPT)} screens drawn with {renderer} -> {WORK}")

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
        print(f"  {start:6.2f} - {end:6.2f}  screen_{i:02d}  {screen}  (timer {timer_text(start)})")
        for c_start, c_end, c_screen, text in cues:
            if c_screen == screen:
                print(f"           {c_start:6.2f} - {c_end:6.2f}    {text}")
    print(f"  total {total:.2f}s\n")

    # Meeting frames: two per screen, mic dot on and off, timer at the screen's start.
    for i, (_, start, _) in enumerate(screen_spans):
        with Image.open(WORK / f"page_{i:02d}.png") as page:
            for state in ("on", "off"):
                compose_meeting_frame(page, timer_text(start), state == "on").save(FRAMES / f"screen_{i:02d}_{state}.png")

    # Video: the two frames of each screen alternate every BLINK seconds for the
    # screen's span, then mux with the audio.
    concat = WORK / "screens.txt"
    lines_out = []
    last = None
    for i, (_, start, end) in enumerate(screen_spans):
        left = end - start
        state = "on"
        while left > 1e-6:
            step = min(BLINK, left)
            last = (FRAMES / f"screen_{i:02d}_{state}.png").resolve()
            lines_out.append(f"file '{last}'")
            lines_out.append(f"duration {step:.3f}")
            left -= step
            state = "off" if state == "on" else "on"
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
    print(f"wrote {MP4.name} ({size_mb:.2f} MB, {codec}, {total:.1f}s, {CANVAS_W}x{CANVAS_H}) and {VTT.name} ({len(cues)} cues), renderer {renderer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
