"""Build the README demo from the onboarding example's reference reading.

Writes four files under docs/:
  demo.gif        an animation of the dashboard filling up (about 12 s, 8 fps, 800 px wide)
  dashboard.png   the finished dashboard, 1600 px wide
  questions.png   the FINDINGS column on its own, top five ranked questions
  scoreboard.png  the footer counters with the found-vs-asked-for bars

Every row on the dashboard is a real row from examples/onboarding/reference/analysis.json,
every frame is a real keyframe from a run of the example, and every transcript line comes
from examples/onboarding/walkthrough.vtt. Nothing is invented.

Usage: .venv/bin/python docs/make_demo.py [OUT_DIR]   (OUT_DIR holds frames/ of a run of the example)
"""
from __future__ import annotations

import base64
import io
import json
import math
import re
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
REF = ROOT / "examples" / "onboarding" / "reference" / "analysis.json"
VTT = ROOT / "examples" / "onboarding" / "walkthrough.vtt"

W, H = 1280, 800
GIF_W, GIF_H = 800, 500
FPS = 8
FRAME_MS = 1000 // FPS

# Facts quoted on the footer. Two readings of the onboarding example, one of complaints.
RECALL_ONBOARDING = (0.93, 0.97)
RECALL_COMPLAINTS = 1.00
COST_PER_HOUR = (1.15, 1.60)
TESTS = 563
MODEL_CALLS = 4

CATEGORY_WEIGHT = {
    "validation rule": 0,
    "permissions": 1,
    "edge case": 2,
    "ambiguity": 3,
    "integration": 4,
    "data": 5,
    "missing information": 6,
}


def mmss(t: float) -> str:
    t = int(t)
    return f"{t // 60:02d}:{t % 60:02d}"


def read_cues(path: Path) -> list[dict]:
    """Return the transcript cues as {start, end, text}."""
    cues = []
    block: list[str] = []
    for line in path.read_text().splitlines() + [""]:
        if line.strip():
            block.append(line.strip())
            continue
        for i, b in enumerate(block):
            m = re.match(r"(\d\d):(\d\d):(\d\d)\.(\d+) --> (\d\d):(\d\d):(\d\d)\.(\d+)", b)
            if m:
                g = [int(x) for x in m.groups()]
                start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
                end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
                text = " ".join(block[i + 1 :])
                text = re.sub(r"<v [^>]*>", "", text).replace("</v>", "").strip()
                cues.append({"start": start, "end": end, "text": text})
        block = []
    return cues


def cue_at(cues: list[dict], t: float) -> int:
    """Index of the cue being spoken at reading timestamp t (timestamps are rounded down)."""
    probe = t + 0.9
    best = 0
    for i, c in enumerate(cues):
        if c["start"] <= probe:
            best = i
    return best


KIND_WEIGHT = {"workflow": 3, "functional": 2, "validation": 1, "data": 0}


def ranked_questions(analysis: dict) -> list[dict]:
    """Most blocking first.

    Questions that hold up more requirements come first. Among equals, rules the system must
    enforce (validation, permissions) come before missing background; then the ones that hold
    up a workflow step (locks, approvals) before the ones that hold up a field; then by time.
    """
    kind = {r["id"]: r["kind"] for r in analysis["requirements"]}
    return sorted(
        analysis["questions"],
        key=lambda q: (
            -len(q["blocks_requirement_ids"]),
            CATEGORY_WEIGHT.get(q["category"], 9),
            -sum(KIND_WEIGHT.get(kind.get(r, ""), 0) for r in q["blocks_requirement_ids"]),
            q["timestamp"],
        ),
    )


def esc(s: object) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


CSS = """
:root{
  --ground:#0D1117; --panel:#10161D; --raised:#151C24; --line:#262E38; --line-soft:#1C232B;
  --ink:#E6EDF3; --muted:#8B96A3; --faint:#5B6570;
  --accent:#58A6FF; --accent-soft:rgba(88,166,255,.13); --accent-line:rgba(88,166,255,.45);
  --green:#3FB950; --green-soft:rgba(63,185,80,.14);
  --amber:#D29922; --amber-soft:rgba(210,153,34,.15);
  --sans:"IBM Plex Sans",-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--ground);color:var(--ink);font:13px/1.4 var(--sans);-webkit-font-smoothing:antialiased}
body{width:1280px;height:800px;overflow:hidden;display:flex;flex-direction:column}
.mono{font-family:var(--mono)}

/* top bar */
.top{height:52px;flex:none;display:flex;align-items:center;gap:22px;padding:0 18px;border-bottom:1px solid var(--line);background:var(--panel)}
.brand{font-weight:600;font-size:17px;letter-spacing:.02em;display:flex;align-items:baseline;gap:8px}
.brand small{font-family:var(--mono);font-size:10.5px;color:var(--faint);font-weight:400}
.rec{font-family:var(--mono);font-size:11.5px;color:var(--muted);border:1px solid var(--line);padding:4px 9px;display:flex;gap:8px;align-items:center}
.rec b{color:var(--ink);font-weight:500}
.rec i{width:6px;height:6px;background:var(--accent);display:inline-block}
.stages{display:flex;align-items:center;margin-left:auto}
.stage{display:flex;align-items:center;gap:7px;font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--faint)}
.stage .dot{width:9px;height:9px;border:1.5px solid var(--faint);border-radius:50%}
.stage.active{color:var(--ink)}
.stage.active .dot{border-color:var(--accent);background:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.stage.done{color:var(--muted)}
.stage.done .dot{border-color:var(--green);background:var(--green)}
.stages .link{width:44px;height:1px;background:var(--line);margin:0 10px}
.stages .link.done{background:var(--green)}
.who{font-size:11.5px;color:var(--muted);margin-left:26px;padding-left:22px;border-left:1px solid var(--line)}
.who b{color:var(--ink);font-weight:500}

/* status line */
.status{height:26px;flex:none;display:flex;align-items:center;gap:9px;padding:0 18px;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:11.5px;color:var(--muted);background:var(--ground)}
.status .led{width:7px;height:7px;border-radius:50%;background:var(--accent)}
.status.done .led{background:var(--green)}
.status.done{color:var(--ink)}
.status .ok{color:var(--green);margin-right:2px}

/* main grid */
.main{flex:1;display:grid;grid-template-columns:500px 370px 410px;min-height:0}
.col{min-height:0;display:flex;flex-direction:column;border-right:1px solid var(--line)}
.col:last-child{border-right:0}
.colhead{height:38px;flex:none;display:flex;align-items:center;gap:10px;padding:0 14px;border-bottom:1px solid var(--line-soft);font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:500}
.colhead .count{font-family:var(--mono);font-size:11px;letter-spacing:0;text-transform:none;color:var(--accent);background:var(--accent-soft);padding:2px 7px}
.colhead .hint{margin-left:auto;text-transform:none;letter-spacing:0;font-weight:400;color:var(--faint);font-size:11px}

/* left: frame + transcript */
.frame{position:relative;margin:12px 14px 0;border:1px solid var(--line);background:#000;flex:none}
.frame img{display:block;width:100%;height:auto}
.chip{position:absolute;left:10px;top:10px;font-family:var(--mono);font-size:11px;color:#fff;background:rgba(13,17,23,.86);border:1px solid rgba(255,255,255,.18);padding:3px 8px;letter-spacing:.02em}
.chip b{color:var(--accent);font-weight:500}
.frame .cursor{position:absolute;left:0;right:0;bottom:0;height:3px;background:var(--line)}
.frame .cursor i{position:absolute;left:0;top:0;bottom:0;background:var(--accent)}
.tlabel{padding:12px 14px 6px;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:500;display:flex;gap:10px}
.tlabel .hint{margin-left:auto;text-transform:none;letter-spacing:0;font-weight:400;color:var(--faint)}
.cues{flex:1;min-height:0;overflow:hidden;padding:0 14px 10px;position:relative}
.cue{display:grid;grid-template-columns:44px 1fr;gap:10px;padding:3px 8px;color:var(--faint);font-size:11.5px;line-height:1.35;border-left:2px solid transparent;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cue .t{font-family:var(--mono);font-size:10.5px;padding-top:1px}
.cue span{overflow:hidden;text-overflow:ellipsis}
.cue.on{color:var(--ink);white-space:normal;border-left-color:var(--accent);background:var(--accent-soft);padding:6px 8px;margin:2px 0;font-size:12.5px}
.cue.on .t{color:var(--accent)}

/* cards */
.list{flex:1;min-height:0;overflow:hidden;padding:10px 12px;display:flex;flex-direction:column;gap:8px}
.card{border:1px solid var(--line);background:var(--raised);padding:9px 11px;flex:none}
.card.hidden{display:none}
.empty{color:var(--faint);font-size:11.5px;padding:10px 2px;border:1px dashed var(--line);text-align:center}
.empty.hidden{display:none}
.card.sel{border-color:var(--accent-line);box-shadow:inset 2px 0 0 var(--accent)}
.card .hd{display:flex;align-items:center;gap:8px;margin-bottom:5px;font-size:11px}
.card .id{font-family:var(--mono);color:var(--accent);font-weight:500;font-size:11.5px}
.card .rank{font-family:var(--mono);color:var(--faint);font-size:11px;min-width:18px}
.card .kind{color:var(--faint)}
.card .time{margin-left:auto;font-family:var(--mono);color:var(--faint);font-size:10.5px}
.pill{font-size:10px;letter-spacing:.05em;text-transform:uppercase;padding:2px 6px;border:1px solid var(--line);color:var(--muted);line-height:1.3}
.pill.high{color:var(--green);border-color:rgba(63,185,80,.4);background:var(--green-soft)}
.pill.medium{color:var(--amber);border-color:rgba(210,153,34,.4);background:var(--amber-soft)}
.pill.low{color:var(--muted);border-color:var(--line)}
.pill.cat{color:var(--amber);border-color:rgba(210,153,34,.4);background:var(--amber-soft)}
.card .txt{font-size:12.5px;line-height:1.38;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.card .quote{margin-top:5px;font-size:11.5px;color:var(--muted);font-style:italic;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card .ft{display:flex;align-items:center;gap:6px;margin-top:6px;font-family:var(--mono);font-size:10.5px;color:var(--faint)}
.card .ft .fc{color:var(--muted);border:1px solid var(--line);padding:1px 6px}
.card .ft .fc b{color:var(--accent);font-weight:500}
.card .ft .holds{color:var(--faint);font-family:var(--sans);font-size:10.5px}
.card .ft .rid{color:var(--accent);border:1px solid var(--accent-line);padding:1px 5px}

/* acceptance criteria under the selected requirement */
.ac{border:1px solid var(--line);border-top:0;background:var(--panel);padding:8px 11px 9px;margin-top:-8px;flex:none}
.ac.hidden{display:none}
.ac .lb{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:500;margin-bottom:6px;display:flex;gap:8px}
.ac .lb span{font-family:var(--mono);color:var(--accent);letter-spacing:0}
.ac .row{display:grid;grid-template-columns:44px 1fr;gap:6px;font-size:11.5px;line-height:1.35;margin-bottom:3px}
.ac .row b{font-weight:500;color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;padding-top:1px}
.ac .sep{height:1px;background:var(--line-soft);margin:6px 0}

/* footer */
.footer{height:106px;flex:none;border-top:1px solid var(--line);background:var(--panel);display:grid;grid-template-columns:640px 640px}
.counters{display:grid;grid-template-columns:repeat(4,1fr);border-right:1px solid var(--line)}
.ctr{padding:16px 18px 0;border-right:1px solid var(--line-soft)}
.ctr:last-child{border-right:0}
.ctr .n{font-family:var(--mono);font-size:24px;font-weight:500;color:var(--ink);line-height:1.1;letter-spacing:-.01em}
.ctr .n small{font-size:14px;color:var(--muted);font-weight:400}
.ctr .l{margin-top:6px;font-size:11px;color:var(--muted);line-height:1.3}
.ctr .l b{color:var(--accent);font-weight:500}
.bars{padding:11px 18px 0}
.bars .lb{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:500;display:flex;gap:14px;align-items:center;margin-bottom:7px}
.bars .lb .leg{margin-left:auto;display:flex;gap:12px;text-transform:none;letter-spacing:0;font-weight:400;color:var(--faint);font-size:10.5px}
.bars .lb .leg i{display:inline-block;width:9px;height:9px;margin-right:5px;vertical-align:-1px}
.bars .lb .leg .a{background:var(--accent)}
.bars .lb .leg .b{background:var(--line)}
.brow{display:grid;grid-template-columns:86px 1fr 74px;gap:12px;align-items:center;margin-bottom:6px;font-size:11.5px}
.brow .name{color:var(--ink)}
.brow .name small{display:block;color:var(--faint);font-size:10px;line-height:1.2}
.brow .track{height:10px;background:var(--line);position:relative}
.brow .fill{position:absolute;left:0;top:0;bottom:0;background:var(--accent)}
.brow .tick{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--ink);opacity:.9}
.brow .v{font-family:var(--mono);font-size:12px;color:var(--ink);text-align:right}

/* alternative layouts used for the crops */
body.focus-findings .main{grid-template-columns:600px}
body.focus-findings .col.left,body.focus-findings .col.right,body.focus-findings .top,body.focus-findings .status,body.focus-findings .footer{display:none}
body.focus-findings .col{border:1px solid var(--line)}
body.focus-findings .card .txt{-webkit-line-clamp:4}
body.scoreboard{width:800px;height:200px}
body.scoreboard .top,body.scoreboard .status,body.scoreboard .main{display:none}
body.scoreboard .footer{height:200px;grid-template-columns:800px;grid-template-rows:92px 108px;border-top:0}
body.scoreboard .counters{border-right:0;border-bottom:1px solid var(--line)}
body.scoreboard .bars{padding-top:14px}
body.scoreboard .brow{margin-bottom:10px}
"""


def page_html(analysis: dict, cues: list[dict], frames_b64: list[str]) -> str:
    reqs = analysis["requirements"]
    acs = analysis["acceptance_criteria"]
    qs = ranked_questions(analysis)
    screens = analysis["screens"]
    screen_name = {s["id"]: s["name"] for s in screens}

    data = {
        "frames": frames_b64,
        "cues": cues,
        "screens": [{"id": s["id"], "name": s["name"], "first_seen": s["first_seen"], "kf": s["keyframe_indexes"][0]} for s in screens],
        "reqs": [
            {
                "id": r["id"], "statement": r["statement"], "quote": r["source_quote"], "t": r["timestamp"],
                "kf": r["keyframe_index"], "kind": r["kind"], "confidence": r["confidence"],
                "screen": f"{r['screen_id']} · {screen_name[r['screen_id']]}",
            }
            for r in reqs
        ],
        "acs": [{"id": a["id"], "rid": a["requirement_id"], "given": a["given"], "when": a["when"], "then": a["then"]} for a in acs],
        "qs": [
            {
                "id": q["id"], "question": q["question"], "t": q["timestamp"], "kf": q["keyframe_index"],
                "category": q["category"], "blocks": q["blocks_requirement_ids"],
            }
            for q in qs
        ],
        "facts": {
            "recall_on": RECALL_ONBOARDING, "recall_co": RECALL_COMPLAINTS, "cost": COST_PER_HOUR, "tests": TESTS,
            "calls": MODEL_CALLS, "nreq": len(reqs), "nq": len(qs), "duration": mmss(math.ceil(cues[-1]["end"])),
        },
    }

    js = r"""
const D = window.DATA;
const $ = (s, el=document) => el.querySelector(s);
const mmss = t => { t = Math.floor(t); return String(Math.floor(t/60)).padStart(2,'0') + ':' + String(t%60).padStart(2,'0'); };
const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const kf = i => `frame ${String(i).padStart(2,'0')}`;

function build(){
  $('#rec-dur').textContent = D.facts.duration;
  $('#cues').innerHTML = D.cues.map((c,i) => `<div class="cue" data-i="${i}"><span class="t">${mmss(c.start)}</span><span>${esc(c.text)}</span></div>`).join('');
  $('#reqs').innerHTML += D.reqs.map((r,i) => `
    <div class="card req hidden" data-i="${i}" data-id="${r.id}">
      <div class="hd"><span class="id">${r.id}</span><span class="kind">${esc(r.kind)}</span><span class="pill ${r.confidence}">${r.confidence}</span><span class="time">${mmss(r.t)}</span></div>
      <div class="txt">${esc(r.statement)}</div>
      <div class="quote">“${esc(r.quote)}”</div>
      <div class="ft"><span class="fc"><b>${kf(r.kf)}</b> · ${mmss(r.t)}</span><span class="holds">${esc(r.screen)}</span></div>
    </div>
    <div class="ac hidden" data-for="${r.id}"></div>`).join('');
  $('#qs').innerHTML += D.qs.map((q,i) => `
    <div class="card q hidden" data-i="${i}">
      <div class="hd"><span class="rank">${String(i+1).padStart(2,'0')}</span><span class="id">${q.id}</span><span class="pill cat">${esc(q.category)}</span><span class="time">${mmss(q.t)}</span></div>
      <div class="txt">${esc(q.question)}</div>
      <div class="ft"><span class="fc"><b>${kf(q.kf)}</b> · ${mmss(q.t)}</span>${q.blocks.length ? `<span class="holds">holds up</span>${q.blocks.map(b => `<span class="rid">${b}</span>`).join('')}` : `<span class="holds">holds up nothing yet · still worth an answer</span>`}</div>
    </div>`).join('');
  for (const r of D.reqs){
    const el = $(`.ac[data-for="${r.id}"]`);
    const mine = D.acs.filter(a => a.rid === r.id);
    el.innerHTML = `<div class="lb">Acceptance criteria <span>${mine.map(a=>a.id).join(' · ')}</span></div>` +
      mine.map(a => `<div class="row"><b>Given</b><span>${esc(a.given)}</span></div><div class="row"><b>When</b><span>${esc(a.when)}</span></div><div class="row"><b>Then</b><span>${esc(a.then)}</span></div>`).join('<div class="sep"></div>');
  }
}

const fmt2 = x => x.toFixed(2);
function setState(s){
  // left: frame, chip, playhead, transcript
  const img = $('#frame-img');
  if (img.dataset.i !== String(s.frame)) { img.src = 'data:image/jpeg;base64,' + D.frames[s.frame]; img.dataset.i = String(s.frame); }
  const scr = D.screens[s.frame];
  $('#chip').innerHTML = `<b>${kf(s.frame)}</b> · ${mmss(s.t)} · ${esc(scr.name)}`;
  const total = D.cues[D.cues.length-1].end;
  $('#cursor').style.width = (100 * Math.min(1, (s.t + 0.9) / total)).toFixed(2) + '%';
  document.querySelectorAll('.cue').forEach(el => el.classList.toggle('on', Number(el.dataset.i) === s.cue));
  const cues = $('#cues'); const on = $('.cue.on');
  cues.scrollTop = Math.max(0, on.offsetTop - cues.offsetTop - 70);

  // stages + status
  ['frames','reading','merge'].forEach((k,i) => {
    const el = $(`#st-${k}`); el.className = 'stage ' + (s.stages[i] || '');
    if (i > 0) $(`#lk-${i}`).className = 'link ' + (s.stages[i-1] === 'done' ? 'done' : '');
  });
  $('#status').className = 'status' + (s.done ? ' done' : '');
  // the activity light pulses while the run is in progress
  const pulse = s.done ? 1 : [1, .6, .3, .6][(s.tick || 0) % 4];
  $('#status .led').style.opacity = pulse;
  document.querySelectorAll('.stage.active .dot').forEach(el => el.style.opacity = pulse);
  $('#status-text').innerHTML = (s.done ? '<span class="ok">✓</span> ' : '') + esc(s.status);

  // requirements
  document.querySelectorAll('.card.req').forEach(el => {
    el.classList.toggle('hidden', Number(el.dataset.i) >= s.req);
    el.classList.toggle('sel', el.dataset.id === s.selReq);
  });
  document.querySelectorAll('.ac').forEach(el => el.classList.toggle('hidden', el.dataset.for !== s.selReq));
  $('#req-count').textContent = s.reqCount ?? s.req;
  $('#req-empty').classList.toggle('hidden', s.req > 0);
  const rl = $('#reqs');
  if (s.selReq) { const c = $(`.card.req[data-id="${s.selReq}"]`); rl.scrollTop = c.offsetTop - rl.offsetTop - 10; } else rl.scrollTop = 0;

  // findings
  document.querySelectorAll('.card.q').forEach(el => {
    el.classList.toggle('hidden', Number(el.dataset.i) >= s.q);
    el.classList.toggle('sel', s.selQ && Number(el.dataset.i) === 0);
  });
  $('#q-count').textContent = s.qCount ?? s.q;
  $('#q-empty').classList.toggle('hidden', s.q > 0);

  // footer counters, t in 0..1
  const t = s.t01;
  const F = D.facts;
  $('#c1').innerHTML = `${fmt2(F.recall_on[0]*t)}<small>–${fmt2(F.recall_on[1]*t)}</small>`;
  $('#c2').textContent = fmt2(F.recall_co*t);
  $('#c3').innerHTML = `$${fmt2(F.cost[0]*t)}<small>–${fmt2(F.cost[1]*t)}</small>`;
  $('#c4').textContent = String(Math.round(F.tests*t));
  $('#b1').style.width = (100*F.recall_on[1]*t).toFixed(1) + '%';
  $('#b1t').style.left = (100*F.recall_on[0]*t).toFixed(1) + '%';
  $('#b1v').textContent = `${fmt2(F.recall_on[0]*t)}–${fmt2(F.recall_on[1]*t)}`;
  $('#b2').style.width = (100*F.recall_co*t).toFixed(1) + '%';
  $('#b2v').textContent = fmt2(F.recall_co*t);
}
window.setState = setState;
build();
"""

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>specto</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{CSS}</style></head><body>
<div class="top">
  <div class="brand">specto <small>v0.6.0</small></div>
  <div class="rec"><i></i><b>walkthrough.mp4</b> · <span id="rec-dur"></span></div>
  <div class="stages">
    <div class="stage" id="st-frames"><span class="dot"></span>Frames</div><div class="link" id="lk-1"></div>
    <div class="stage" id="st-reading"><span class="dot"></span>Reading</div><div class="link" id="lk-2"></div>
    <div class="stage" id="st-merge"><span class="dot"></span>Merge</div>
  </div>
  <div class="who">Northwind Onboarding · expert <b>Oderic Falstow</b></div>
</div>
<div class="status" id="status"><span class="led"></span><span id="status-text"></span></div>
<div class="main">
  <div class="col left">
    <div class="frame"><img id="frame-img" alt=""><div class="chip" id="chip"></div><div class="cursor"><i id="cursor"></i></div></div>
    <div class="tlabel">Transcript <span class="hint">line spoken at this frame</span></div>
    <div class="cues" id="cues"></div>
  </div>
  <div class="col mid">
    <div class="colhead">Findings <span class="count" id="q-count">0</span><span class="hint">questions for the expert · most blocking first</span></div>
    <div class="list" id="qs"><div class="empty" id="q-empty">No questions yet · they are ranked once the reading is complete</div></div>
  </div>
  <div class="col right">
    <div class="colhead">Requirements <span class="count" id="req-count">0</span><span class="hint">each with its quote and frame</span></div>
    <div class="list" id="reqs"><div class="empty" id="req-empty">No requirements yet · waiting for the first reading call</div></div>
  </div>
</div>
<div class="footer">
  <div class="counters">
    <div class="ctr"><div class="n" id="c1"></div><div class="l"><b>recall</b> onboarding example, two readings</div></div>
    <div class="ctr"><div class="n" id="c2"></div><div class="l"><b>recall</b> complaints example</div></div>
    <div class="ctr"><div class="n" id="c3"></div><div class="l"><b>cost</b> per recorded hour</div></div>
    <div class="ctr"><div class="n" id="c4"></div><div class="l"><b>tests</b> offline, run in CI</div></div>
  </div>
  <div class="bars">
    <div class="lb">Found vs asked-for <span class="leg"><span><i class="a"></i>found by specto</span><span><i class="b"></i>asked for by the reference</span></span></div>
    <div class="brow"><div class="name">Onboarding<small>6 screens · 01:51</small></div><div class="track"><div class="fill" id="b1"></div><div class="tick" id="b1t"></div></div><div class="v" id="b1v"></div></div>
    <div class="brow"><div class="name">Complaints<small>second example</small></div><div class="track"><div class="fill" id="b2"></div></div><div class="v" id="b2v"></div></div>
  </div>
</div>
<script>window.DATA = {json.dumps(data)};</script>
<script>{js}</script>
</body></html>"""


def timeline(analysis: dict, cues: list[dict]) -> list[dict]:
    """The sequence of dashboard states, one per GIF frame."""
    reqs = analysis["requirements"]
    qs = ranked_questions(analysis)
    screens = analysis["screens"]
    nreq, nq = len(reqs), len(qs)
    final_status = (
        f"Analysis complete in {MODEL_CALLS} model calls · {nreq} requirements · {nq} questions · every row linked to its frame"
    )

    def base(**kw) -> dict:
        s = {"frame": 0, "t": 0.0, "cue": 0, "stages": ["", "", ""], "status": "", "done": False,
             "req": 0, "q": 0, "selReq": None, "selQ": False, "t01": 0.0}
        s.update(kw)
        return s

    def at(t: float, kf: int, **kw) -> dict:
        return base(frame=kf, t=t, cue=cue_at(cues, t), **kw)

    states: list[dict] = []
    hold = lambda s, n: states.extend([dict(s)] * n)

    # 1. the six keyframes, with the line spoken at each, about 0.75 s each
    for i, scr in enumerate(screens):
        hold(at(scr["first_seen"], scr["keyframe_indexes"][0], stages=["active", "", ""],
                status=f"Extracting keyframes · {i + 1} of {len(screens)} screens found · {mmss(math.ceil(cues[-1]['end']))} of video"), 6)

    # 2. the progress bar walks on
    last = screens[-1]
    hold(at(last["first_seen"], last["keyframe_indexes"][0], stages=["done", "", ""], status="6 keyframes kept · 14 transcript lines aligned"), 2)
    hold(at(last["first_seen"], last["keyframe_indexes"][0], stages=["done", "active", ""], status="Reading frames with the transcript · call 1 of 4"), 3)

    # 3. requirement cards appear one by one, the frame follows each one
    for n in range(1, 9):
        r = reqs[n - 1]
        hold(at(r["timestamp"], r["keyframe_index"], stages=["done", "active", ""], req=n,
                status=f"Reading · call 2 of 4 · {n} requirements so far"), 2)
    r = reqs[8]
    hold(at(r["timestamp"], r["keyframe_index"], stages=["done", "active", ""], req=nreq,
            status=f"Reading · call 2 of 4 · {nreq} requirements"), 2)

    # 4. findings appear, ranked
    for n in range(1, 6):
        q = qs[n - 1]
        hold(at(q["timestamp"], q["keyframe_index"], stages=["done", "active", ""], req=nreq, q=n,
                status=f"Reading · call 3 of 4 · {nreq} requirements · {n} questions"), 2)
    q = qs[5]
    hold(at(q["timestamp"], q["keyframe_index"], stages=["done", "active", ""], req=nreq, q=nq,
            status=f"Reading · call 3 of 4 · {nreq} requirements · {nq} questions"), 2)

    # 5. merge: the top finding is selected and linked to the requirement it holds up
    top = qs[0]
    sel_req = next(r for r in reqs if r["id"] == top["blocks_requirement_ids"][-1])
    hold(at(top["timestamp"], top["keyframe_index"], stages=["done", "done", "active"], req=nreq, q=nq, selQ=True,
            status="Merge · call 4 of 4 · ranking questions by what they hold up"), 3)
    linked = at(sel_req["timestamp"], sel_req["keyframe_index"], stages=["done", "done", "active"], req=nreq, q=nq,
                selQ=True, selReq=sel_req["id"], status=f"Merge · call 4 of 4 · {top['id']} holds up {' and '.join(top['blocks_requirement_ids'])}")
    hold(linked, 3)

    # 6. footer counters tick up
    for k in range(1, 9):
        s = dict(linked, t01=k / 8)
        if k == 8:
            s.update(stages=["done", "done", "done"], status=final_status, done=True)
        states.append(s)

    # 7. hold on the finished state
    hold(states[-1], 16)
    return [dict(s, tick=i) for i, s in enumerate(states)]


def render(out_dir: Path) -> None:
    from playwright.sync_api import sync_playwright

    analysis = json.loads(REF.read_text())
    cues = read_cues(VTT)
    frames_b64 = [base64.b64encode((out_dir / "frames" / f"frame_{i:04d}.jpg").read_bytes()).decode() for i in range(6)]
    html = page_html(analysis, cues, frames_b64)
    states = timeline(analysis, cues)
    final = states[-1]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=2)
        page.set_content(html, wait_until="load")
        page.evaluate("document.fonts.ready.then(() => true)")
        page.wait_for_timeout(600)

        # the animation
        raw: list[Image.Image] = []
        for s in states:
            page.evaluate("s => window.setState(s)", s)
            png = page.screenshot(clip={"x": 0, "y": 0, "width": W, "height": H})
            raw.append(Image.open(io.BytesIO(png)).convert("RGB").resize((GIF_W, GIF_H), Image.LANCZOS))
        write_gif(raw, DOCS / "demo.gif")

        # the finished dashboard
        page.evaluate("s => window.setState(s)", final)
        png = page.screenshot(clip={"x": 0, "y": 0, "width": W, "height": H})
        Image.open(io.BytesIO(png)).convert("RGB").resize((1600, 1000), Image.LANCZOS).save(DOCS / "dashboard.png", optimize=True)

        # the findings column on its own, top five
        page.evaluate("document.body.classList.add('focus-findings')")
        page.evaluate("s => window.setState(s)", dict(final, q=5, qCount=final["q"]))
        box = page.evaluate("(() => { const c = document.querySelector('.col.mid'); const r = c.getBoundingClientRect(); const cards = [...document.querySelectorAll('.card.q:not(.hidden)')]; const b = cards[cards.length-1].getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: b.bottom - r.y + 12}; })()")
        page.screenshot(path=str(DOCS / "questions.png"), clip={"x": box["x"], "y": box["y"], "width": box["w"], "height": box["h"]})
        page.evaluate("document.body.classList.remove('focus-findings')")

        # the scoreboard: counters plus the found-vs-asked-for bars
        page.evaluate("document.body.classList.add('scoreboard')")
        page.evaluate("s => window.setState(s)", final)
        page.screenshot(path=str(DOCS / "scoreboard.png"), clip={"x": 0, "y": 0, "width": 800, "height": 200})
        browser.close()

    for name in ("demo.gif", "dashboard.png", "questions.png", "scoreboard.png"):
        f = DOCS / name
        with Image.open(f) as im:
            im.load()
            frames = getattr(im, "n_frames", 1)
            print(f"{name}: {im.size[0]}x{im.size[1]}, {f.stat().st_size / 1024:.0f} KB" + (f", {frames} frames" if frames > 1 else ""))


def write_gif(frames: list[Image.Image], target: Path) -> None:
    """One shared palette for every frame so Pillow can store only what changed between frames."""
    sample = frames[::6] + [frames[-1]]
    sheet = Image.new("RGB", (GIF_W * len(sample), GIF_H))
    for i, f in enumerate(sample):
        sheet.paste(f, (i * GIF_W, 0))
    palette = sheet.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    quantised = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    durations = [FRAME_MS] * len(quantised)
    quantised[0].save(target, save_all=True, append_images=quantised[1:], duration=durations, loop=0, optimize=True, disposal=1)


if __name__ == "__main__":
    render(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "out" / "real2")
