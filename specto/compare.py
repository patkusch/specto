"""Compare two finished analyses of what should be the same journey.

merge.py folds several sessions into one analysis; this is the opposite
operation: it keeps two finished analyses apart and reports what is
different between them. Useful after a system change ("we changed the
system, did the requirements change too?") or when two experts described
the same process and their readings do not agree.

Matching is by normalized text, the same idea merge.py's `_dedupe` uses to
fold duplicates together: whitespace collapsed, case folded. An item whose
normalized text is identical in both analyses is the same item and is left
out of every list. An item with no match at all in the normalized text of
the other analysis is added (only in B) or removed (only in A). What is left
over is matched up by how similar the text is (Python's difflib), so a
requirement that was reworded but is clearly the same requirement is
reported as "changed", with both versions, rather than as one removed and
one unrelated added item. No new recording.json is written; this is a diff,
not a merge.
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from specto.model import Analysis
from specto.timefmt import mmss

Kind = Literal["screen", "field", "action", "requirement", "question"]

KINDS: tuple[Kind, ...] = ("screen", "field", "action", "requirement", "question")
KIND_LABELS: dict[Kind, str] = {
    "screen": "Screens", "field": "Fields", "action": "Actions",
    "requirement": "Requirements", "question": "Questions",
}

# How similar two remaining items' normalized text must be (difflib ratio,
# 0 to 1) to call them the same item reworded rather than two unrelated
# items, one removed and one added.
CHANGE_THRESHOLD = 0.5


# ------------------------------------------------------------------ the result


class DiffItem(BaseModel):
    """One item found in only one of the two analyses."""

    kind: Kind
    id: str
    text: str
    frame: Optional[str] = Field(default=None, description="A citation like 'frame 3 @ 00:12', when the item carries one")


class ChangedItem(BaseModel):
    """The same item in both analyses, near-matched by text but not identical."""

    kind: Kind
    id_a: str
    text_a: str
    frame_a: Optional[str] = None
    id_b: str
    text_b: str
    frame_b: Optional[str] = None


class Comparison(BaseModel):
    """What compare_analyses found between two analyses, label_a first."""

    label_a: str
    label_b: str
    added: list[DiffItem] = Field(default_factory=list, description="In label_b but not label_a")
    removed: list[DiffItem] = Field(default_factory=list, description="In label_a but not label_b")
    changed: list[ChangedItem] = Field(default_factory=list, description="In both, but reworded")

    @property
    def total(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)


# ------------------------------------------------------------------ matching


def _norm(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _frame_citation(index: Optional[int], timestamp: Optional[float]) -> Optional[str]:
    if index is None:
        return None
    return f"frame {index} @ {mmss(timestamp or 0.0)}"


Item = tuple[str, str, Optional[str]]  # (id, text, frame citation)


def _items_for(analysis: Analysis, kind: Kind) -> list[Item]:
    """Every item of one kind as (id, text, frame citation), in the analysis's own order."""
    if kind == "screen":
        return [(s.id, s.name, _frame_citation(s.keyframe_indexes[0], s.first_seen) if s.keyframe_indexes else None)
                for s in analysis.screens]
    if kind == "field":
        return [(f.id, f.label, _frame_citation(f.keyframe_index, f.timestamp)) for f in analysis.fields]
    if kind == "action":
        return [(a.id, a.description, _frame_citation(a.keyframe_index, a.timestamp)) for a in analysis.actions]
    if kind == "requirement":
        return [(r.id, r.statement, _frame_citation(r.keyframe_index, r.timestamp)) for r in analysis.requirements]
    if kind == "question":
        return [(q.id, q.question, _frame_citation(q.keyframe_index, q.timestamp)) for q in analysis.questions]
    raise ValueError(kind)  # pragma: no cover - KINDS is the only caller


def _match(items_a: list[Item], items_b: list[Item]) -> tuple[list[Item], list[Item], list[tuple[Item, Item]]]:
    """Split two item lists into (removed, added, changed pairs).

    An exact normalized-text match is dropped from both sides first (the
    same item, unchanged). What is left is paired off, best similarity
    first, as long as the match clears CHANGE_THRESHOLD; anything still
    unpaired after that is a real addition or removal.
    """
    remaining_a: dict[str, Item] = {item[0]: item for item in items_a}
    remaining_b: dict[str, Item] = {item[0]: item for item in items_b}

    by_norm_text: dict[str, list[str]] = {}
    for id_b, item_b in remaining_b.items():
        by_norm_text.setdefault(_norm(item_b[1]), []).append(id_b)
    for id_a, item_a in list(remaining_a.items()):
        bucket = by_norm_text.get(_norm(item_a[1]))
        if bucket:
            id_b = bucket.pop(0)
            del remaining_a[id_a]
            del remaining_b[id_b]

    candidates: list[tuple[float, str, str]] = []
    for id_a, item_a in remaining_a.items():
        for id_b, item_b in remaining_b.items():
            ratio = difflib.SequenceMatcher(None, _norm(item_a[1]), _norm(item_b[1])).ratio()
            if ratio >= CHANGE_THRESHOLD:
                candidates.append((ratio, id_a, id_b))
    candidates.sort(key=lambda c: -c[0])

    changed: list[tuple[Item, Item]] = []
    for _ratio, id_a, id_b in candidates:
        if id_a in remaining_a and id_b in remaining_b:
            changed.append((remaining_a.pop(id_a), remaining_b.pop(id_b)))

    return list(remaining_a.values()), list(remaining_b.values()), changed


def compare_analyses(analysis_a: Analysis, analysis_b: Analysis, label_a: str = "before", label_b: str = "after") -> Comparison:
    """What changed between two analyses of the same (or a before/after) journey.

    Symmetric and simple: every one of the five kinds of item is matched on
    its own, by normalized text, then near-matched by similarity for
    anything left over. Nothing is written to disk; call compare_report_markdown
    or compare_html on the result.
    """
    added: list[DiffItem] = []
    removed: list[DiffItem] = []
    changed: list[ChangedItem] = []
    for kind in KINDS:
        items_a = _items_for(analysis_a, kind)
        items_b = _items_for(analysis_b, kind)
        removed_items, added_items, changed_pairs = _match(items_a, items_b)
        removed += [DiffItem(kind=kind, id=id_, text=text, frame=frame) for id_, text, frame in removed_items]
        added += [DiffItem(kind=kind, id=id_, text=text, frame=frame) for id_, text, frame in added_items]
        for (id_a, text_a, frame_a), (id_b, text_b, frame_b) in changed_pairs:
            changed.append(ChangedItem(kind=kind, id_a=id_a, text_a=text_a, frame_a=frame_a,
                                       id_b=id_b, text_b=text_b, frame_b=frame_b))
    return Comparison(label_a=label_a, label_b=label_b, added=added, removed=removed, changed=changed)


# ------------------------------------------------------------------ Markdown report


def _md_diff_list(items: list[DiffItem]) -> list[str]:
    lines: list[str] = []
    for kind in KINDS:
        group = [i for i in items if i.kind == kind]
        if not group:
            continue
        lines.append(f"### {KIND_LABELS[kind]}")
        lines.append("")
        for item in group:
            tail = f" ({item.frame})" if item.frame else ""
            lines.append(f"- **{item.id}** {item.text}{tail}")
        lines.append("")
    return lines


def _md_changed_list(items: list[ChangedItem], label_a: str, label_b: str) -> list[str]:
    lines: list[str] = []
    for kind in KINDS:
        group = [i for i in items if i.kind == kind]
        if not group:
            continue
        lines.append(f"### {KIND_LABELS[kind]}")
        lines.append("")
        for item in group:
            tail_a = f" ({item.frame_a})" if item.frame_a else ""
            tail_b = f" ({item.frame_b})" if item.frame_b else ""
            lines.append(f"- {label_a} **{item.id_a}**: {item.text_a}{tail_a}")
            lines.append(f"  {label_b} **{item.id_b}**: {item.text_b}{tail_b}")
        lines.append("")
    return lines


def compare_report_markdown(comparison: Comparison) -> str:
    """A Markdown report: Added, Removed and Changed sections, each citing its source."""
    c = comparison
    lines = [f"# Comparing {c.label_a} with {c.label_b}", ""]
    if c.total == 0:
        lines.append("No differences found.")
        return "\n".join(lines) + "\n"

    lines.append(f"{len(c.added)} added, {len(c.removed)} removed, {len(c.changed)} changed.")
    lines.append("")

    lines.append(f"## Added in {c.label_b}")
    lines.append("")
    lines += _md_diff_list(c.added) if c.added else ["Nothing added.", ""]

    lines.append(f"## Removed from {c.label_a}")
    lines.append("")
    lines += _md_diff_list(c.removed) if c.removed else ["Nothing removed.", ""]

    lines.append("## Changed")
    lines.append("")
    lines += _md_changed_list(c.changed, c.label_a, c.label_b) if c.changed else ["Nothing changed.", ""]

    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ HTML view


def _esc(value: object) -> str:
    import html as _html
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


def _html_diff_list(title: str, items: list[DiffItem], empty_text: str) -> str:
    parts = [f"<section>\n<h2>{_esc(title)}</h2>\n"]
    if not items:
        parts.append(f'<p class="muted">{_esc(empty_text)}</p>\n')
    for kind in KINDS:
        group = [i for i in items if i.kind == kind]
        if not group:
            continue
        parts.append(f"<h3>{_esc(KIND_LABELS[kind])}</h3>\n<ul>\n")
        for item in group:
            tail = f' <span class="when">{_esc(item.frame)}</span>' if item.frame else ""
            parts.append(f'<li><span class="id">{_esc(item.id)}</span> {_esc(item.text)}{tail}</li>\n')
        parts.append("</ul>\n")
    parts.append("</section>\n")
    return "".join(parts)


def _html_changed_section(comparison: Comparison) -> str:
    c = comparison
    parts = ['<section>\n<h2>Changed</h2>\n']
    if not c.changed:
        parts.append('<p class="muted">Nothing changed.</p>\n')
    for kind in KINDS:
        group = [i for i in c.changed if i.kind == kind]
        if not group:
            continue
        parts.append(f"<h3>{_esc(KIND_LABELS[kind])}</h3>\n"
                     f'<table>\n<thead><tr><th>{_esc(c.label_a)}</th><th>{_esc(c.label_b)}</th></tr></thead>\n<tbody>\n')
        for item in group:
            a = f'<span class="id">{_esc(item.id_a)}</span> {_esc(item.text_a)}'
            if item.frame_a:
                a += f' <span class="when">{_esc(item.frame_a)}</span>'
            b = f'<span class="id">{_esc(item.id_b)}</span> {_esc(item.text_b)}'
            if item.frame_b:
                b += f' <span class="when">{_esc(item.frame_b)}</span>'
            parts.append(f"<tr><td>{a}</td><td>{b}</td></tr>\n")
        parts.append("</tbody>\n</table>\n")
    parts.append("</section>\n")
    return "".join(parts)


# Styled to match html_report.py's dashboard: same tokens, same look and feel,
# but a plain static page (no lightbox, no localStorage) since a diff has
# nothing for a viewer to fill in.
CSS = """
:root { --ink:#1f2328; --muted:#6a737d; --line:#d8dde3; --soft:#f3f5f7; --link:#0b5cad; }
* { box-sizing:border-box; }
html { color-scheme:light; }
body { margin:0; color:var(--ink); background:#fff; font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width:1000px; margin:0 auto; padding:24px 24px 64px; }
h1 { font-size:1.7em; margin:0 0 8px; }
h2 { font-size:1.3em; margin:36px 0 10px; padding-bottom:6px; border-bottom:1px solid var(--line); }
h3 { font-size:1em; margin:16px 0 6px; color:var(--muted); text-transform:uppercase; letter-spacing:0.03em; }
.muted { color:var(--muted); }
.totals { color:var(--muted); margin:0 0 16px; }
.id { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:0.9em; color:var(--muted); }
.when { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:0.85em; color:var(--muted); white-space:nowrap; }
ul { padding-left:20px; margin:4px 0 12px; }
li { margin:4px 0; }
table { border-collapse:collapse; width:100%; margin:6px 0 12px; font-size:0.95em; }
th, td { text-align:left; vertical-align:top; padding:8px 10px; border:1px solid var(--line); width:50%; }
th { background:var(--soft); font-weight:600; }
""".strip()


def compare_html(comparison: Comparison) -> str:
    """A self-contained static HTML page: Added, Removed and Changed sections."""
    c = comparison
    title = f"Comparing {c.label_a} with {c.label_b}"
    parts = [
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_esc(title)}</title>\n<style>\n{CSS}\n</style>\n</head>\n<body>\n<main>\n"
        f"<h1>{_esc(title)}</h1>\n"
    ]
    if c.total == 0:
        parts.append('<p class="totals">No differences found.</p>\n')
    else:
        parts.append(f'<p class="totals">{len(c.added)} added, {len(c.removed)} removed, {len(c.changed)} changed.</p>\n')
        parts.append(_html_diff_list(f"Added in {c.label_b}", c.added, "Nothing added."))
        parts.append(_html_diff_list(f"Removed from {c.label_a}", c.removed, "Nothing removed."))
        parts.append(_html_changed_section(c))
    parts.append("</main>\n</body>\n</html>\n")
    return "".join(parts)


# ------------------------------------------------------------------ entry point


def compare_dirs(dir_a: Path | str, dir_b: Path | str, out_dir: Path | str,
                 label_a: str = "before", label_b: str = "after") -> tuple[Comparison, Path, Path]:
    """Load analysis.json from both folders, compare them, and write compare.md
    and compare.html into out_dir. Returns the comparison and the two paths."""
    from specto.merge import load_run

    analysis_a, _recording_a = load_run(dir_a)
    analysis_b, _recording_b = load_run(dir_b)
    comparison = compare_analyses(analysis_a, analysis_b, label_a=label_a, label_b=label_b)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "compare.md"
    html_path = out_dir / "compare.html"
    md_path.write_text(compare_report_markdown(comparison), encoding="utf-8")
    html_path.write_text(compare_html(comparison), encoding="utf-8")
    return comparison, md_path, html_path
