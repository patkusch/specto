"""Stage 3, fourth and fifth outputs: plain XHTML pages for Confluence and SharePoint.

Confluence's "Import from Word or HTML" turns ordinary HTML headings and
tables into wiki content, and its storage format is itself a dialect of
XHTML, so a simple, well-formed XHTML file with no script and no external
stylesheet pastes in cleanly. SharePoint's page import (Word/HTML) reads the
same kind of file but is more at home with plain markup, so the content is
written twice: once with a Confluence-flavoured wrapper, once with a plainer
one. Both wrappers are built from the same section and table functions, so
the two files can never drift apart in content, only in the shell around it.

This is a different deliverable from report.html: no frame images (an import
target does not want a multi-megabyte page), no localStorage, no
contenteditable, no JavaScript at all, just static tables an importer can
render as wiki pages. A frame is cited as text, e.g. "frame 7 @ 01:23", the
same way the Jira and Azure DevOps exports cite it.
"""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Optional

from specto.export import (
    blocks_text, criteria_by_requirement, questions_to_ask_first, screen_names, summary_rows,
)
from specto.glossary import GLOSSARY_HEADERS, glossary_rows, glossary_with_findings
from specto.model import Analysis, Recording
from specto.timefmt import mmss

TABLE_STYLE = "border-collapse:collapse;width:100%;margin:8px 0 24px;"
TH_STYLE = "border:1px solid #cccccc;background:#f0f0f0;padding:6px 8px;text-align:left;vertical-align:top;"
TD_STYLE = "border:1px solid #cccccc;padding:6px 8px;text-align:left;vertical-align:top;"


def esc(value: Any) -> str:
    """HTML-escape anything; None becomes an empty string."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _required_text(required: Optional[bool]) -> str:
    if required is None:
        return "Unknown"
    return "Yes" if required else "No"


def _frame_text(index: Optional[int], timestamp: Optional[float]) -> str:
    """A frame cited as text, e.g. 'frame 7 @ 01:23'; empty when there is no frame."""
    if index is None:
        return ""
    return f"frame {index} @ {mmss(timestamp or 0.0)}"


# ------------------------------------------------------------------ the shared table builder


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    """One plain <table>: a header row, then a row per item, every cell escaped.

    Shared by both flavours (and by every section below), so a change to how a
    table looks only has to be made once.
    """
    head_cells = "".join(f'<th style="{TH_STYLE}">{esc(h)}</th>' for h in headers)
    body_rows = []
    for row in rows:
        cells = "".join(f'<td style="{TD_STYLE}">{esc(v)}</td>' for v in row)
        body_rows.append(f"<tr>{cells}</tr>")
    return (f'<table style="{TABLE_STYLE}"><thead><tr>{head_cells}</tr></thead>'
            f'<tbody>{"".join(body_rows)}</tbody></table>\n')


def _heading(level: int, text: str, anchor: Optional[str] = None) -> str:
    attr = f' id="{esc(anchor)}"' if anchor else ""
    return f"<h{level}{attr}>{esc(text)}</h{level}>\n"


# ------------------------------------------------------------------ sections
#
# Each of these covers the same ground as one part of the Markdown report
# (export_markdown in export.py): summary, journey, screens with their
# fields and actions, requirements with their acceptance criteria,
# questions, glossary. Kept as plain tables rather than the report's prose,
# because that is what an importer turns into a clean wiki page.


def _summary_section(analysis: Analysis, recording: Recording) -> str:
    rows = [[item, value] for item, value in summary_rows(analysis, recording) if item not in ("Title", "Summary")]
    return (_heading(1, analysis.title) + f"<p>{esc(analysis.summary)}</p>\n" +
            _table(["Item", "Value"], rows))


def _journey_section(analysis: Analysis) -> str:
    names = screen_names(analysis)
    rows = [[j.order, names.get(j.screen_id, j.screen_id), j.description, j.actor or "",
             mmss(j.timestamp), _frame_text(j.keyframe_index, j.timestamp)] for j in analysis.journey]
    return _heading(2, "Journey", "journey") + _table(["Order", "Screen", "What happens", "Who", "Time", "Frame"], rows)


def _screens_section(analysis: Analysis) -> str:
    names = screen_names(analysis)
    screen_rows = [[s.id, s.name, s.purpose, mmss(s.first_seen), len(s.field_ids), len(s.action_ids)]
                   for s in analysis.screens]
    field_rows = [[f.id, names.get(f.screen_id, f.screen_id), f.label, f.field_type, _required_text(f.required),
                   f.example_value or "", f.source, mmss(f.timestamp), _frame_text(f.keyframe_index, f.timestamp),
                   f.notes or ""] for f in analysis.fields]
    action_rows = [[a.id, names.get(a.screen_id, a.screen_id), a.description, a.control or "",
                    names.get(a.leads_to_screen_id, a.leads_to_screen_id) if a.leads_to_screen_id else "",
                    mmss(a.timestamp), _frame_text(a.keyframe_index, a.timestamp)] for a in analysis.actions]
    return (
        _heading(2, "Screens", "screens") +
        _table(["Id", "Name", "Purpose", "First seen", "Fields", "Actions"], screen_rows) +
        _heading(3, "Data fields", "data-fields") +
        _table(["Id", "Screen", "Label", "Type", "Required", "Example value", "Source", "Time", "Frame", "Notes"], field_rows) +
        _heading(3, "Actions", "actions") +
        _table(["Id", "Screen", "Action", "Control", "Leads to", "Time", "Frame"], action_rows)
    )


def _requirements_section(analysis: Analysis) -> str:
    names = screen_names(analysis)
    criteria = criteria_by_requirement(analysis)
    requirement_text = {r.id: r.statement for r in analysis.requirements}
    req_rows = [[r.id, r.statement, r.kind, r.priority, r.confidence,
                 names.get(r.screen_id, r.screen_id) if r.screen_id else "", r.rationale or "", r.source_quote,
                 mmss(r.timestamp), _frame_text(r.keyframe_index, r.timestamp),
                 ", ".join(ac.id for ac in criteria.get(r.id, []))] for r in analysis.requirements]
    ac_rows = [[ac.id, ac.requirement_id, requirement_text.get(ac.requirement_id, ""), ac.given, ac.when, ac.then,
                mmss(ac.timestamp), _frame_text(ac.keyframe_index, ac.timestamp)] for ac in analysis.acceptance_criteria]
    return (
        _heading(2, "Requirements", "requirements") +
        _table(["Id", "Statement", "Kind", "Priority", "Confidence", "Screen", "Rationale", "Source quote",
                "Time", "Frame", "Acceptance criteria"], req_rows) +
        _heading(3, "Acceptance criteria", "acceptance-criteria") +
        _table(["Id", "Requirement id", "Requirement", "Given", "When", "Then", "Time", "Frame"], ac_rows)
    )


def _questions_section(analysis: Analysis) -> str:
    names = screen_names(analysis)
    rows = [[q.id, q.question, q.why_it_matters, q.category, names.get(q.screen_id, q.screen_id) if q.screen_id else "",
             blocks_text(q), q.context_quote or "", mmss(q.timestamp), _frame_text(q.keyframe_index, q.timestamp),
             q.answer or "", q.status if q.status != "open" else ""] for q in questions_to_ask_first(analysis)]
    return (_heading(2, "SME questions", "questions") +
            "<p>Questions that hold up the most requirements come first.</p>\n" +
            _table(["Id", "Question", "Why it matters", "Category", "Screen", "Blocks", "What was said",
                    "Time", "Frame", "Answer", "Status"], rows))


def _glossary_section(analysis: Analysis) -> str:
    entries, _findings = glossary_with_findings(analysis)
    rows = glossary_rows(entries)[1:]
    for row in rows:
        row[4] = f"frame {row[4]}"
    return _heading(2, "Glossary", "glossary") + _table(GLOSSARY_HEADERS, rows)


def _body(analysis: Analysis, recording: Recording) -> str:
    """Every section, in the order the Markdown report covers them."""
    return "".join([
        _summary_section(analysis, recording),
        _journey_section(analysis),
        _screens_section(analysis),
        _requirements_section(analysis),
        _questions_section(analysis),
        _glossary_section(analysis),
    ])


# ------------------------------------------------------------------ the two wrappers


def _wrap_confluence(title: str, body: str) -> str:
    """XHTML with an XML namespace, matching storage format's own XHTML lineage."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8" />\n'
        '<meta name="generator" content="specto (Confluence import)" />\n'
        f"<title>{esc(title)}</title>\n"
        "</head>\n"
        f"<body>\n{body}</body>\n"
        "</html>\n"
    )


def _wrap_sharepoint(title: str, body: str) -> str:
    """Plainer XHTML, no namespace, matching what SharePoint's Word/HTML import expects."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8" />\n'
        '<meta name="generator" content="specto (SharePoint import)" />\n'
        f"<title>{esc(title)}</title>\n"
        "</head>\n"
        f"<body>\n{body}</body>\n"
        "</html>\n"
    )


# ------------------------------------------------------------------ entry points


def export_confluence_page(analysis: Analysis, recording: Recording, out_dir: Path,
                           filename: str = "confluence.html") -> Path:
    """Write a Confluence-storage-format-friendly XHTML page in out_dir and return its path.

    Paste this into Confluence with Import from Word/HTML (or Space Tools,
    Content Tools, Import), or straight into a page's Insert menu, HTML.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    page = _wrap_confluence(analysis.title, _body(analysis, recording))
    path = out_dir / filename
    path.write_text(page, encoding="utf-8")
    return path


def export_sharepoint_page(analysis: Analysis, recording: Recording, out_dir: Path,
                           filename: str = "sharepoint.html") -> Path:
    """Write a SharePoint-page-import-friendly XHTML page in out_dir and return its path.

    In SharePoint, add a page, choose the Word/HTML import (or a File Viewer
    web part pointed at this file), and pick sharepoint.html.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    page = _wrap_sharepoint(analysis.title, _body(analysis, recording))
    path = out_dir / filename
    path.write_text(page, encoding="utf-8")
    return path


def export_wiki_pages(analysis: Analysis, recording: Recording, out_dir: Path) -> dict[str, Path]:
    """Write both wiki import files; return {"confluence_html": path, "sharepoint_html": path}."""
    return {
        "confluence_html": export_confluence_page(analysis, recording, out_dir),
        "sharepoint_html": export_sharepoint_page(analysis, recording, out_dir),
    }
