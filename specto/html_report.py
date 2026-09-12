"""Stage 3, third output: one HTML file with the frames inside it.

The workbook and the Markdown report link to the still images in `frames/`,
so they break when someone moves the file on its own. This report embeds every
still as a base64 JPEG, so the one file can be emailed or dropped in a chat and
opens anywhere with no server and no network.

Each frame is encoded exactly once (a thumbnail and a larger copy) and reused
wherever it appears, through an SVG `<use>` reference, so a frame that shows up
in twenty transcript rows costs the file one copy, not twenty. Clicking a
thumbnail opens the larger copy in a CSS-only lightbox (`:target`), so the file
reads fine with JavaScript off. The only script is a few lines for the filter
boxes and the Escape key, and printing works without them.
"""
from __future__ import annotations

import base64
import html
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image

from specto.export import criteria_by_requirement, frame_for_segment, frame_path, screen_names, summary_rows
from specto.lint import Finding, format_findings, lint_analysis
from specto.model import Analysis, Recording
from specto.timefmt import mmss

LARGE_WIDTH = 1280
THUMB_QUALITY = 70
LARGE_QUALITY = 50


@dataclass
class FrameImage:
    """One keyframe, encoded twice: a thumbnail for the page and a larger copy for the lightbox."""

    index: int
    timestamp: float
    thumb: str  # base64 JPEG
    thumb_w: int
    thumb_h: int
    large: str  # base64 JPEG
    large_w: int
    large_h: int


# ------------------------------------------------------------------ frames


def _jpeg_b64(img: Image.Image, max_width: int, quality: int) -> tuple[str, int, int]:
    copy = img.copy()
    copy.thumbnail((max_width, max_width * 10))
    buf = io.BytesIO()
    copy.save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii"), copy.width, copy.height


def encode_frames(recording: Recording, out_dir: Path, thumb_width: int,
                  large_width: int = LARGE_WIDTH) -> dict[int, FrameImage]:
    """Read every keyframe that exists on disk and encode it; frames that are missing are left out."""
    frames: dict[int, FrameImage] = {}
    for kf in recording.keyframes:
        path = Path(out_dir) / kf.path
        if not path.is_file():
            continue
        try:
            with Image.open(path) as img:
                rgb = img.convert("RGB")
        except OSError:
            continue
        thumb, tw, th = _jpeg_b64(rgb, thumb_width, THUMB_QUALITY)
        large, lw, lh = _jpeg_b64(rgb, large_width, LARGE_QUALITY)
        frames[kf.index] = FrameImage(kf.index, kf.timestamp, thumb, tw, th, large, lw, lh)
    return frames


def frame_alt(index: int, timestamp: float) -> str:
    """The alt text on a thumbnail, e.g. 'frame 7 at 01:23'."""
    return f"frame {index} at {mmss(timestamp)}"


# ------------------------------------------------------------------ escaping


def esc(value: object) -> str:
    """HTML-escape anything; None becomes an empty string."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _required_text(required: Optional[bool]) -> str:
    if required is None:
        return "Unknown"
    return "Yes" if required else "No"


# ------------------------------------------------------------------ the page


class _Page:
    """Builds the HTML piece by piece; every method appends escaped markup to `parts`."""

    def __init__(self, analysis: Analysis, recording: Recording, frames: dict[int, FrameImage]):
        self.analysis = analysis
        self.recording = recording
        self.frames = frames
        self.names = screen_names(analysis)
        self.criteria = criteria_by_requirement(analysis)
        self.checks = lint_analysis(analysis)
        self.parts: list[str] = []

    def add(self, markup: str) -> None:
        self.parts.append(markup)

    # ---- small pieces

    def screen(self, screen_id: Optional[str]) -> str:
        if not screen_id:
            return ""
        return esc(self.names.get(screen_id, screen_id))

    def thumb(self, index: int, timestamp: float) -> str:
        """A clickable thumbnail for a frame, or a grey placeholder when the image is missing."""
        alt = esc(frame_alt(index, timestamp))
        f = self.frames.get(index)
        if f is None:
            return f'<span class="thumb placeholder" title="{alt}">frame {index}<br><small>no image</small></span>'
        return (
            f'<a class="thumb" href="#big-{index}" title="{alt}">'
            f'<svg role="img" aria-label="{alt}" viewBox="0 0 {f.thumb_w} {f.thumb_h}" '
            f'width="{f.thumb_w // 2}" height="{f.thumb_h // 2}"><title>{alt}</title>'
            f'<use href="#t{index}"/></svg></a>'
        )

    def time_and_thumb(self, index: int, timestamp: float) -> str:
        return f'<span class="when">{esc(mmss(timestamp))}</span> {self.thumb(index, timestamp)}'

    def pill(self, kind: str, text: str) -> str:
        return f'<span class="pill {esc(kind)} {esc(kind)}-{esc(text)}">{esc(text)}</span>'

    def findings(self, item_id: str) -> str:
        found: list[Finding] = self.checks.get(item_id, [])
        if not found:
            return ""
        return f'<p class="check">Writing check: {esc(format_findings(found))}</p>'

    # ---- sections

    def head(self) -> None:
        title = esc(self.analysis.title)
        self.add(f"<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
                 f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
                 f"<title>{title}</title>\n<style>\n{CSS}\n</style>\n</head>\n<body>\n")

    def defs(self) -> None:
        """Every thumbnail once, as an SVG image the page reuses by id."""
        if not self.frames:
            return
        self.add('<svg width="0" height="0" class="defs" aria-hidden="true"><defs>\n')
        for f in self.frames.values():
            self.add(f'<image id="t{f.index}" width="{f.thumb_w}" height="{f.thumb_h}" '
                     f'href="data:image/jpeg;base64,{f.thumb}"/>\n')
        self.add("</defs></svg>\n")

    def toc(self) -> None:
        self.add('<nav class="toc"><a href="#summary">Summary</a> <a href="#journey">Journey</a> '
                 '<a href="#flow">Flow</a> <a href="#screens">Screens</a> <a href="#requirements">Requirements</a> '
                 '<a href="#questions">SME questions</a> <a href="#transcript">Transcript</a></nav>\n')

    def summary(self) -> None:
        a, r = self.analysis, self.recording
        self.add(f'<section id="summary">\n<h1>{esc(a.title)}</h1>\n<p class="lead">{esc(a.summary)}</p>\n<dl class="facts">\n')
        for item, value in summary_rows(a, r):
            if item in ("Title", "Summary"):
                continue
            self.add(f"<dt>{esc(item)}</dt><dd>{esc(value)}</dd>\n")
        self.add("</dl>\n</section>\n")

    def journey(self) -> None:
        self.add('<section id="journey">\n<h2>Journey</h2>\n')
        if not self.analysis.journey:
            self.add("<p class=\"muted\">No steps recorded.</p>\n")
        else:
            self.add('<ol class="journey">\n')
            for j in self.analysis.journey:
                who = f' <span class="muted">({esc(j.actor)})</span>' if j.actor else ""
                self.add(f'<li value="{j.order}"><div><b>{self.screen(j.screen_id)}</b>{who}: {esc(j.description)} '
                         f'<span class="when">{esc(mmss(j.timestamp))}</span></div>{self.thumb(j.keyframe_index, j.timestamp)}</li>\n')
            self.add("</ol>\n")
        self.add("</section>\n")

    def flow(self) -> None:
        from .flow import build_flow, flow_list, flow_svg

        flow = build_flow(self.analysis)
        self.add('<section id="flow">\n<h2>Screen flow</h2>\n')
        if not flow.nodes:
            self.add('<p class="muted">No screens recorded.</p>\n</section>\n')
            return
        self.add('<p class="muted">Boxes are screens in the order the expert reached them; labelled arrows are the actions that led from one to the next.</p>\n')
        self.add('<div class="flow">' + flow_svg(flow) + '</div>\n')
        self.add('<ol class="flowlist">' + "".join(f"<li>{esc(line)}</li>" for line in flow_list(flow)) + "</ol>\n")
        self.add("</section>\n")

    def screens(self) -> None:
        a = self.analysis
        by_index = {kf.index: kf.timestamp for kf in self.recording.keyframes}
        fields_by_screen = {s.id: [f for f in a.fields if f.screen_id == s.id] for s in a.screens}
        actions_by_screen = {s.id: [x for x in a.actions if x.screen_id == s.id] for s in a.screens}
        self.add('<section id="screens">\n<h2>Screens</h2>\n')
        if not a.screens:
            self.add("<p class=\"muted\">No screens recorded.</p>\n")
        for s in a.screens:
            self.add(f'<article class="screen" id="{esc(s.id)}">\n<h3><span class="id">{esc(s.id)}</span> {esc(s.name)}</h3>\n'
                     f'<p>{esc(s.purpose)} <span class="muted">First seen {esc(mmss(s.first_seen))}.</span></p>\n')
            if s.keyframe_indexes:
                self.add('<div class="strip">' + " ".join(
                    self.thumb(i, by_index.get(i, s.first_seen)) for i in s.keyframe_indexes) + "</div>\n")
            self.add("<h4>Data fields</h4>\n")
            fields = fields_by_screen.get(s.id, [])
            if not fields:
                self.add("<p class=\"muted\">None recorded.</p>\n")
            else:
                self.add("<table>\n<thead><tr><th>Id</th><th>Label</th><th>Type</th><th>Required</th>"
                         "<th>Example</th><th>Source</th><th>Notes</th></tr></thead>\n<tbody>\n")
                for f in fields:
                    self.add(f'<tr id="{esc(f.id)}"><td class="id">{esc(f.id)}</td><td>{esc(f.label)}</td><td>{esc(f.field_type)}</td>'
                             f"<td>{_required_text(f.required)}</td><td>{esc(f.example_value)}</td>"
                             f"<td>{esc(f.source)}</td><td>{esc(f.notes)}</td></tr>\n")
                self.add("</tbody>\n</table>\n")
            self.add("<h4>Actions</h4>\n")
            actions = actions_by_screen.get(s.id, [])
            if not actions:
                self.add("<p class=\"muted\">None recorded.</p>\n")
            else:
                self.add("<ul class=\"actions\">\n")
                for x in actions:
                    control = f' <span class="muted">[{esc(x.control)}]</span>' if x.control else ""
                    leads = f" &rarr; {self.screen(x.leads_to_screen_id)}" if x.leads_to_screen_id else ""
                    self.add(f'<li id="{esc(x.id)}"><span class="id">{esc(x.id)}</span> {esc(x.description)}{control}{leads} '
                             f'<span class="when">{esc(mmss(x.timestamp))}</span></li>\n')
                self.add("</ul>\n")
            self.add("</article>\n")
        self.add("</section>\n")

    def requirements(self) -> None:
        a = self.analysis
        self.add('<section id="requirements">\n<h2>Requirements</h2>\n'
                 '<p class="filter"><input type="search" data-filter="#requirements .req" '
                 'placeholder="Filter requirements by any word" aria-label="Filter requirements"></p>\n')
        if not a.requirements:
            self.add("<p class=\"muted\">No requirements recorded.</p>\n")
        for r in a.requirements:
            where = f' <span class="muted">on {self.screen(r.screen_id)}</span>' if r.screen_id else ""
            self.add(f'<article class="req" id="{esc(r.id)}">\n'
                     f'<header><span class="id">{esc(r.id)}</span> {self.pill("kind", r.kind)} '
                     f'{self.pill("priority", r.priority)} {self.pill("confidence", r.confidence)}{where}</header>\n'
                     f"<h3>{esc(r.statement)}</h3>\n")
            if r.rationale:
                self.add(f"<p>{esc(r.rationale)}</p>\n")
            self.add(f'<figure class="quote"><blockquote>{esc(r.source_quote)}</blockquote>'
                     f"<figcaption>{self.time_and_thumb(r.keyframe_index, r.timestamp)}</figcaption></figure>\n")
            self.add(self.findings(r.id))
            acs = self.criteria.get(r.id, [])
            self.add("<h4>Acceptance criteria</h4>\n")
            if not acs:
                self.add("<p class=\"muted\">None yet.</p>\n")
            else:
                self.add("<table class=\"criteria\">\n<thead><tr><th>Id</th><th>Given</th><th>When</th><th>Then</th><th>Frame</th></tr></thead>\n<tbody>\n")
                for ac in acs:
                    check = self.findings(ac.id)
                    self.add(f'<tr id="{esc(ac.id)}"><td class="id">{esc(ac.id)}</td><td>{esc(ac.given)}</td><td>{esc(ac.when)}</td>'
                             f"<td>{esc(ac.then)}{check}</td><td>{self.time_and_thumb(ac.keyframe_index, ac.timestamp)}</td></tr>\n")
                self.add("</tbody>\n</table>\n")
            self.add("</article>\n")
        self.add("</section>\n")

    def questions(self) -> None:
        a = self.analysis
        self.add('<section id="questions">\n<h2>SME questions</h2>\n'
                 '<p class="muted">The Answer column can be typed into in the browser; print to PDF to keep the answers.</p>\n')
        if not a.questions:
            self.add("<p class=\"muted\">No questions recorded.</p>\n</section>\n")
            return
        self.add("<table class=\"questions\">\n<thead><tr><th>Id</th><th>Question</th><th>Why it matters</th><th>Category</th>"
                 "<th>Screen</th><th>What was said</th><th>Frame</th><th>Answer</th></tr></thead>\n<tbody>\n")
        for q in a.questions:
            self.add(f'<tr id="{esc(q.id)}"><td class="id">{esc(q.id)}</td><td>{esc(q.question)}</td><td>{esc(q.why_it_matters)}</td>'
                     f"<td>{esc(q.category)}</td><td>{self.screen(q.screen_id)}</td><td>{esc(q.context_quote)}</td>"
                     f"<td>{self.time_and_thumb(q.keyframe_index, q.timestamp)}</td>"
                     f'<td class="answer" contenteditable="true" aria-label="Answer to {esc(q.id)}">{esc(q.answer or "")}</td></tr>\n')
        self.add("</tbody>\n</table>\n</section>\n")

    def transcript(self) -> None:
        r = self.recording
        self.add('<section id="transcript">\n<h2>Transcript</h2>\n'
                 '<p class="filter"><input type="search" data-filter="#transcript tbody tr" '
                 'placeholder="Filter the transcript by any word" aria-label="Filter transcript"></p>\n')
        if not r.segments:
            self.add("<p class=\"muted\">Nothing transcribed.</p>\n</section>\n")
            return
        self.add("<table class=\"transcript\">\n<thead><tr><th>Time</th><th>Speaker</th><th>Text</th><th>Frame</th></tr></thead>\n<tbody>\n")
        for seg in r.segments:
            ref = frame_for_segment(r, seg)
            frame = self.thumb(ref.index, ref.timestamp) if ref else ""
            self.add(f'<tr><td class="when">{esc(mmss(seg.start))}</td><td>{esc(seg.speaker)}</td>'
                     f"<td>{esc(seg.text)}</td><td>{frame}</td></tr>\n")
        self.add("</tbody>\n</table>\n</section>\n")

    def lightboxes(self) -> None:
        """The larger copy of every frame, hidden until its thumbnail is clicked."""
        for f in self.frames.values():
            alt = esc(frame_alt(f.index, f.timestamp))
            self.add(f'<figure class="big" id="big-{f.index}"><a class="backdrop" href="#_" aria-label="Close"></a>'
                     f'<div><img alt="{alt}" width="{f.large_w}" height="{f.large_h}" src="data:image/jpeg;base64,{f.large}">'
                     f'<figcaption>{alt} <a href="#_">close</a></figcaption></div></figure>\n')

    def tail(self) -> None:
        self.add(f"<script>\n{JS}\n</script>\n</body>\n</html>\n")

    def render(self) -> str:
        self.head()
        self.defs()
        self.toc()
        self.add('<main>\n')
        self.summary()
        self.journey()
        self.flow()
        self.screens()
        self.requirements()
        self.questions()
        self.transcript()
        self.add("</main>\n")
        self.lightboxes()
        self.tail()
        return "".join(self.parts)


CSS = """
:root { --ink:#1f2328; --muted:#6a737d; --line:#d8dde3; --soft:#f3f5f7; --link:#0b5cad; }
* { box-sizing:border-box; }
html { color-scheme:light; }
body { margin:0; color:var(--ink); background:#fff; font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:0 24px 64px; }
a { color:var(--link); }
h1 { font-size:1.9em; margin:24px 0 8px; }
h2 { font-size:1.4em; margin:48px 0 12px; padding-bottom:6px; border-bottom:1px solid var(--line); }
h3 { font-size:1.1em; margin:12px 0 6px; }
h4 { font-size:0.85em; text-transform:uppercase; letter-spacing:0.04em; color:var(--muted); margin:16px 0 6px; }
p { margin:6px 0; }
.lead { font-size:1.05em; }
.flow { margin:12px 0; overflow-x:auto; }
.flowlist { font-size:0.9em; color:var(--muted); }
.muted { color:var(--muted); }
.id { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:0.9em; color:var(--muted); }
.when { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:0.85em; color:var(--muted); white-space:nowrap; }
.toc { position:sticky; top:0; z-index:5; background:#fff; border-bottom:1px solid var(--line); padding:10px 24px; display:flex; gap:18px; flex-wrap:wrap; }
.toc a { text-decoration:none; font-weight:600; }
.toc a:hover { text-decoration:underline; }
.defs { position:absolute; }
dl.facts { display:grid; grid-template-columns:max-content 1fr; gap:4px 16px; margin:12px 0; }
dl.facts dt { color:var(--muted); }
dl.facts dd { margin:0; }
table { border-collapse:collapse; width:100%; margin:6px 0 12px; font-size:0.95em; }
th, td { text-align:left; vertical-align:top; padding:6px 8px; border:1px solid var(--line); }
th { background:var(--soft); font-weight:600; }
.thumb { display:inline-block; vertical-align:top; }
.thumb svg { display:block; width:240px; max-width:100%; height:auto; border:1px solid var(--line); border-radius:3px; background:var(--soft); }
td .thumb svg, .strip .thumb svg, .criteria .thumb svg { width:160px; }
.transcript .thumb svg { width:120px; }
.placeholder { width:160px; min-height:90px; padding:8px; border:1px dashed var(--line); border-radius:3px; background:var(--soft); color:var(--muted); font-size:0.85em; text-align:center; }
.strip { display:flex; flex-wrap:wrap; gap:8px; margin:8px 0; }
ol.journey { padding-left:28px; }
ol.journey li { display:flex; gap:16px; align-items:flex-start; margin:10px 0; padding-bottom:10px; border-bottom:1px solid var(--soft); }
ol.journey li > div { flex:1; }
ul.actions { padding-left:20px; }
article.screen, article.req { border:1px solid var(--line); border-radius:6px; padding:12px 16px; margin:14px 0; }
article.req header { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.pill { display:inline-block; font-size:0.75em; padding:1px 8px; border-radius:999px; border:1px solid var(--line); background:var(--soft); color:var(--ink); }
.pill.priority-must { background:#fde8e8; border-color:#f5b5b5; }
.pill.priority-should { background:#fff4dd; border-color:#f3d28a; }
.pill.confidence-high { background:#e3f4e6; border-color:#a9d9b3; }
.pill.confidence-low { background:#fde8e8; border-color:#f5b5b5; }
figure.quote { margin:8px 0; display:flex; gap:16px; align-items:flex-start; }
figure.quote blockquote { flex:1; margin:0; padding:6px 12px; border-left:3px solid var(--line); color:#3b4248; font-style:italic; }
figure.quote figcaption { display:flex; flex-direction:column; gap:4px; align-items:flex-start; }
.check { color:var(--muted); font-size:0.85em; margin:4px 0 0; }
td .check { margin-top:6px; }
.answer { min-width:180px; background:#fffdf3; }
.answer:focus { outline:2px solid var(--link); }
.filter input { width:100%; max-width:420px; padding:6px 10px; border:1px solid var(--line); border-radius:4px; font:inherit; }
.hide { display:none !important; }
figure.big { display:none; position:fixed; inset:0; z-index:20; margin:0; background:rgba(20,22,25,0.85); align-items:center; justify-content:center; }
figure.big:target { display:flex; }
figure.big .backdrop { position:absolute; inset:0; }
figure.big > div { position:relative; max-width:96vw; max-height:96vh; display:flex; flex-direction:column; align-items:center; gap:6px; }
figure.big img { max-width:96vw; max-height:88vh; width:auto; height:auto; border:1px solid #fff; background:#fff; }
figure.big figcaption { color:#fff; font-size:0.9em; }
figure.big figcaption a { color:#fff; margin-left:12px; }
@media print {
  .toc, .filter, figure.big, script { display:none !important; }
  main { max-width:none; padding:0; }
  article.screen, article.req, tr { break-inside:avoid; }
  .thumb svg { width:180px; }
  .answer { min-height:48px; }
  a { color:inherit; text-decoration:none; }
}
""".strip()

JS = """
document.querySelectorAll('input[data-filter]').forEach(function (box) {
  var rows = document.querySelectorAll(box.getAttribute('data-filter'));
  box.addEventListener('input', function () {
    var q = box.value.trim().toLowerCase();
    rows.forEach(function (row) {
      row.classList.toggle('hide', q !== '' && row.textContent.toLowerCase().indexOf(q) === -1);
    });
  });
});
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape' && location.hash.indexOf('#big-') === 0) { location.hash = '#_'; }
});
""".strip()


# ------------------------------------------------------------------ entry point


def export_html(analysis: Analysis, recording: Recording, out_dir: Path, filename: str = "report.html",
                thumb_width: int = 480, large_width: int = LARGE_WIDTH) -> Path:
    """Write the analysis as one self-contained HTML file in out_dir and return its path.

    Frames are read from `out_dir / keyframe.path`, resized with Pillow to
    `thumb_width` for the page and at most `large_width` (1280) for the
    lightbox, and embedded as base64 JPEG. A frame that is not on disk becomes
    a grey placeholder carrying its index.

    Sixty 1280x720 screen stills come to roughly 4 MB; pass a smaller
    `large_width` (say 1024) to bring a long recording under that.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = encode_frames(recording, out_dir, thumb_width, large_width)
    page = _Page(analysis, recording, frames).render()
    path = out_dir / filename
    path.write_text(page, encoding="utf-8")
    return path
