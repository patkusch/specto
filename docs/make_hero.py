"""Build docs/hero.png: a frame from the example recording on the left and,
on the right, three rows specto wrote from that moment (a requirement, an
acceptance criterion, a question), taken from the reference reading.

Usage: .venv/bin/python docs/make_hero.py [OUT_DIR]   (OUT_DIR holds frames/ of a run of the example)
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "examples" / "onboarding" / "reference" / "analysis.json"


def excerpts(analysis: dict) -> tuple[dict, dict, dict]:
    req = next(r for r in analysis["requirements"] if "postcode" in r["statement"].lower() and r["confidence"] == "high")
    crit = next(c for c in analysis["acceptance_criteria"] if c["requirement_id"] == req["id"])
    question = max(analysis["questions"], key=lambda q: len(q["blocks_requirement_ids"]))
    return req, crit, question


def page_html(frame_png_b64: str, req: dict, crit: dict, question: dict) -> str:
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
    body{{margin:0;background:#F4F6F8;font:15px/1.45 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:#1B2733}}
    .wrap{{width:800px;display:grid;grid-template-columns:400px 400px;height:420px}}
    .left{{background:#1B2733;display:flex;align-items:center;justify-content:center;position:relative}}
    .left img{{width:384px;border-radius:4px;box-shadow:0 8px 24px rgba(0,0,0,.4)}}
    .left .cap{{position:absolute;left:12px;top:10px;color:#C9D2DB;font-size:11px;letter-spacing:.06em;text-transform:uppercase}}
    .right{{padding:14px 16px;display:grid;gap:10px;align-content:start}}
    .row{{background:#fff;border:1px solid #D9DFE6;border-radius:5px;padding:9px 12px}}
    .k{{font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:#5E6B78;margin-bottom:3px}}
    .k b{{color:#0B5C8F;font-weight:600}}
    .t{{font-size:13px}}
    .q{{font-size:12px;color:#5E6B78;margin-top:4px;font-style:italic}}
    .arrow{{position:absolute;right:-1px;top:50%;width:0;height:0;border:12px solid transparent;border-left-color:#1B2733;transform:translateY(-50%);z-index:2}}
    </style></head><body><div class="wrap">
    <div class="left"><div class="cap">Frame {req['keyframe_index']} of the walkthrough · 00:{int(req['timestamp']):02d}</div><img src="data:image/jpeg;base64,{frame_png_b64}"><div class="arrow"></div></div>
    <div class="right">
      <div class="row"><div class="k">Requirement <b>{esc(req['id'])}</b> · frame {req['keyframe_index']} @ 00:{int(req['timestamp']):02d}</div><div class="t">{esc(req['statement'])}</div><div class="q">“{esc(req['source_quote'])}”</div></div>
      <div class="row"><div class="k">Acceptance criterion <b>{esc(crit['id'])}</b></div><div class="t"><b>Given</b> {esc(crit['given'])} <b>When</b> {esc(crit['when'])} <b>Then</b> {esc(crit['then'])}</div></div>
      <div class="row"><div class="k">Question for the expert <b>{esc(question['id'])}</b> · holds up {esc(', '.join(question['blocks_requirement_ids']))}</div><div class="t">{esc(question['question'])}</div></div>
    </div></div></body></html>"""


def main(out_dir: Path) -> Path:
    from playwright.sync_api import sync_playwright

    analysis = json.loads(REF.read_text())
    req, crit, question = excerpts(analysis)
    frame = out_dir / "frames" / f"frame_{req['keyframe_index']:04d}.jpg"
    b64 = base64.b64encode(frame.read_bytes()).decode()
    html = page_html(b64, req, crit, question)
    target = ROOT / "docs" / "hero.png"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 800, "height": 420}, device_scale_factor=2)
        page.set_content(html)
        page.wait_for_timeout(200)
        page.screenshot(path=str(target), clip={"x": 0, "y": 0, "width": 800, "height": 420})
        browser.close()
    print(f"wrote {target} ({target.stat().st_size // 1024} KB)")
    return target


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "out" / "real2")
