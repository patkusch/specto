"""Stage 3: write the analysis out as a spreadsheet and a Markdown report.

Both files sit in the output folder next to the `frames/` folder, so every
frame link is a relative path like `frames/frame_0007.jpg` and keeps working
when the whole folder is zipped up and sent to someone else.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from specto.lint import format_findings, lint_analysis, summarize
from specto.model import Analysis, Recording, TranscriptSegment
from specto.timefmt import mmss

HEADER_FILL = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")
HEADER_FONT = Font(bold=True)
LINK_FONT = Font(color="0563C1", underline="single")
TOP_LEFT = Alignment(vertical="top")
TOP_LEFT_WRAP = Alignment(vertical="top", wrap_text=True)

MAX_COLUMN_WIDTH = 70
MIN_COLUMN_WIDTH = 8
WRAP_FROM_WIDTH = 30


@dataclass
class FrameRef:
    """A cell that should become a link to a still frame."""

    index: int
    timestamp: float


# ------------------------------------------------------------------ frame links


def frame_path(recording: Recording, index: int) -> Optional[str]:
    """The relative image path for a keyframe index, or None if we do not know it."""
    for kf in recording.keyframes:
        if kf.index == index:
            return kf.path
    return None


def frame_label(index: int, timestamp: float) -> str:
    """The text shown for a frame link, e.g. 'frame 7 @ 01:23'."""
    return f"frame {index} @ {mmss(timestamp)}"


def frame_markdown(recording: Recording, index: int, timestamp: float) -> str:
    """The bracketed frame link that ends every item in the Markdown report."""
    label = frame_label(index, timestamp)
    path = frame_path(recording, index)
    if path is None:
        return f"({label})"
    return f"([{label}]({path}))"


def frame_for_segment(recording: Recording, segment: TranscriptSegment) -> Optional[FrameRef]:
    """Find the keyframe that was on screen when a transcript segment started."""
    for moment in recording.moments:
        if moment.start <= segment.start <= moment.end:
            return FrameRef(moment.keyframe_index, segment.start)
    return None


# ---------------------------------------------------------------- sheet writing


def write_sheet(wb: Workbook, title: str, headers: list[str], rows: list[list[Any]], recording: Recording) -> Worksheet:
    """Add one sheet: a styled header row, the data rows, frozen header, filter and widths."""
    ws = wb.create_sheet(title=title)
    ws.append(headers)
    for row in rows:
        ws.append([_plain_value(v) for v in row])
        r = ws.max_row
        for c, value in enumerate(row, start=1):
            if isinstance(value, FrameRef):
                _link_cell(ws.cell(row=r, column=c), value, recording)
    _style_sheet(ws, len(headers))
    return ws


def _plain_value(value: Any) -> Any:
    """What goes in the cell before links and styles are applied."""
    if isinstance(value, FrameRef):
        return frame_label(value.index, value.timestamp)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None:
        return ""
    return value


def _link_cell(cell: Any, ref: FrameRef, recording: Recording) -> None:
    """Turn a cell into a blue underlined link to the frame image, if the frame is known."""
    path = frame_path(recording, ref.index)
    if path is None:
        cell.value = f"frame {ref.index}"
        return
    cell.value = frame_label(ref.index, ref.timestamp)
    cell.hyperlink = path
    cell.font = LINK_FONT


def _style_sheet(ws: Worksheet, n_cols: int) -> None:
    """Bold filled header, frozen top row, auto-filter, column widths, wrapping, top alignment."""
    for c in range(1, n_cols + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = TOP_LEFT
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(n_cols)}{max(ws.max_row, 1)}"

    for c in range(1, n_cols + 1):
        longest = 0
        for r in range(1, ws.max_row + 1):
            value = ws.cell(row=r, column=c).value
            if value is None:
                continue
            for line in str(value).splitlines() or [""]:
                longest = max(longest, len(line))
        width = min(max(longest + 2, MIN_COLUMN_WIDTH), MAX_COLUMN_WIDTH)
        ws.column_dimensions[get_column_letter(c)].width = width
        wrap = width >= WRAP_FROM_WIDTH
        for r in range(2, ws.max_row + 1):
            cell = ws.cell(row=r, column=c)
            if cell.font.underline:
                cell.alignment = TOP_LEFT
            else:
                cell.alignment = TOP_LEFT_WRAP if wrap else TOP_LEFT


# ------------------------------------------------------------------ lookups


def screen_names(analysis: Analysis) -> dict[str, str]:
    """Map screen id to screen name; unknown ids fall back to the id itself."""
    return {s.id: s.name for s in analysis.screens}


def _name(names: dict[str, str], screen_id: Optional[str]) -> str:
    if not screen_id:
        return ""
    return names.get(screen_id, screen_id)


def _required_text(required: Optional[bool]) -> str:
    if required is None:
        return "Unknown"
    return "Yes" if required else "No"


def criteria_by_requirement(analysis: Analysis) -> dict[str, list]:
    """Group acceptance criteria under their requirement id, keeping their order."""
    grouped: dict[str, list] = {}
    for ac in analysis.acceptance_criteria:
        grouped.setdefault(ac.requirement_id, []).append(ac)
    return grouped


# ------------------------------------------------------------------ the workbook


def export_xlsx(analysis: Analysis, recording: Recording, out_dir: Path, filename: str = "analysis.xlsx") -> Path:
    """Write the whole analysis to one workbook in out_dir and return its path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    names = screen_names(analysis)
    criteria = criteria_by_requirement(analysis)
    requirement_text = {r.id: r.statement for r in analysis.requirements}
    checks = lint_analysis(analysis)

    wb = Workbook()
    wb.remove(wb.active)

    _summary_sheet(wb, analysis, recording, out_dir)

    write_sheet(
        wb, "Journey",
        ["Order", "Screen", "What happens", "Who", "Time", "Frame"],
        [[j.order, _name(names, j.screen_id), j.description, j.actor, mmss(j.timestamp),
          FrameRef(j.keyframe_index, j.timestamp)] for j in analysis.journey],
        recording,
    )

    screen_rows = []
    for s in analysis.screens:
        first = FrameRef(s.keyframe_indexes[0], s.first_seen) if s.keyframe_indexes else None
        screen_rows.append([
            s.id, s.name, s.purpose, mmss(s.first_seen), first,
            ", ".join(str(i) for i in s.keyframe_indexes),
            len(s.field_ids), len(s.action_ids),
        ])
    write_sheet(
        wb, "Screens",
        ["Id", "Name", "Purpose", "First seen", "Frame", "Frame indexes", "Fields", "Actions"],
        screen_rows, recording,
    )

    write_sheet(
        wb, "Data Fields",
        ["Id", "Screen", "Label", "Type", "Required", "Example value", "Source", "Time", "Frame", "Notes"],
        [[f.id, _name(names, f.screen_id), f.label, f.field_type, _required_text(f.required), f.example_value,
          f.source, mmss(f.timestamp), FrameRef(f.keyframe_index, f.timestamp), f.notes] for f in analysis.fields],
        recording,
    )

    write_sheet(
        wb, "Actions",
        ["Id", "Screen", "Action", "Control", "Leads to", "Time", "Frame"],
        [[a.id, _name(names, a.screen_id), a.description, a.control, _name(names, a.leads_to_screen_id),
          mmss(a.timestamp), FrameRef(a.keyframe_index, a.timestamp)] for a in analysis.actions],
        recording,
    )

    write_sheet(
        wb, "Requirements",
        ["Id", "Statement", "Kind", "Priority", "Confidence", "Screen", "Rationale", "Source quote",
         "Time", "Frame", "Acceptance criteria", "Writing check"],
        [[r.id, r.statement, r.kind, r.priority, r.confidence, _name(names, r.screen_id), r.rationale,
          r.source_quote, mmss(r.timestamp), FrameRef(r.keyframe_index, r.timestamp),
          ", ".join(ac.id for ac in criteria.get(r.id, [])),
          format_findings(checks.get(r.id, []))] for r in analysis.requirements],
        recording,
    )

    write_sheet(
        wb, "Acceptance Criteria",
        ["Id", "Requirement id", "Requirement", "Given", "When", "Then", "Time", "Frame", "Writing check"],
        [[ac.id, ac.requirement_id, requirement_text.get(ac.requirement_id, ""), ac.given, ac.when, ac.then,
          mmss(ac.timestamp), FrameRef(ac.keyframe_index, ac.timestamp),
          format_findings(checks.get(ac.id, []))] for ac in analysis.acceptance_criteria],
        recording,
    )

    write_sheet(
        wb, "SME Questions",
        ["Id", "Question", "Why it matters", "Category", "Screen", "What was said", "Time", "Frame",
         "Answer", "Status"],
        [[q.id, q.question, q.why_it_matters, q.category, _name(names, q.screen_id), q.context_quote,
          mmss(q.timestamp), FrameRef(q.keyframe_index, q.timestamp), q.answer,
          q.status if q.status != "open" else ""] for q in analysis.questions],
        recording,
    )

    write_sheet(
        wb, "Transcript",
        ["Time", "Speaker", "Text", "Frame"],
        [[mmss(seg.start), seg.speaker, seg.text, frame_for_segment(recording, seg)] for seg in recording.segments],
        recording,
    )

    from .glossary import GLOSSARY_HEADERS, glossary_rows, glossary_with_findings

    entries, _findings = glossary_with_findings(analysis)
    rows = glossary_rows(entries)[1:]
    for row, entry in zip(rows, entries):
        row[4] = FrameRef(entry.keyframe_index, entry.first_seen)
    write_sheet(wb, "Glossary", GLOSSARY_HEADERS, rows, recording)

    from .pii import PII_HEADERS, load_ocr_text, pii_rows, scan_recording

    hits = scan_recording(recording, analysis, load_ocr_text(out_dir))
    prows = pii_rows(hits)[1:]
    for row, hit in zip(prows, hits):
        if hit.keyframe_index is not None:
            row[4] = FrameRef(hit.keyframe_index, hit.timestamp or 0.0)
    write_sheet(wb, "Personal Data", PII_HEADERS, prows, recording)

    path = out_dir / filename
    wb.save(path)
    return path


def personal_data_line(analysis: Analysis, recording: Recording, out_dir: Optional[Path]) -> str:
    """One sentence on the personal data the recording captured, for the Summary sheet."""
    from .pii import load_ocr_text, pii_summary, scan_recording

    ocr = load_ocr_text(out_dir) if out_dir else {}
    return pii_summary(scan_recording(recording, analysis, ocr))


def naming_check_line(analysis: Analysis) -> str:
    """One sentence on the glossary's naming findings, for the Summary sheet."""
    from .glossary import glossary_with_findings

    entries, findings = glossary_with_findings(analysis)
    if not findings:
        return f"{len(entries)} terms in the glossary, no naming clashes found"
    return f"{len(entries)} terms in the glossary; {len(findings)} naming " + ("clash" if len(findings) == 1 else "clashes") + " to check on the Glossary sheet"


def summary_rows(analysis: Analysis, recording: Recording, out_dir: Optional[Path] = None) -> list[tuple[str, Any]]:
    """The item/value pairs shown on the Summary sheet and at the top of the report."""
    rows: list[tuple[str, Any]] = [
        ("Title", analysis.title),
        ("Summary", analysis.summary),
        ("Actors", ", ".join(analysis.actors)),
        ("Recording", recording.source),
        ("Duration", mmss(recording.duration)),
        ("Transcript source", recording.transcript_source),
        ("Screens", len(analysis.screens)),
        ("Data fields", len(analysis.fields)),
        ("Actions", len(analysis.actions)),
        ("Journey steps", len(analysis.journey)),
        ("Requirements", len(analysis.requirements)),
        ("Acceptance criteria", len(analysis.acceptance_criteria)),
        ("Questions", len(analysis.questions)),
        ("Writing check", summarize(lint_analysis(analysis))),
        ("Naming check", naming_check_line(analysis)),
        ("Personal data", personal_data_line(analysis, recording, out_dir)),
    ]
    if analysis.usage is not None:
        u = analysis.usage
        rows += [
            ("Model", u.model),
            ("Model calls", u.calls),
            ("Input tokens", u.input_tokens),
            ("Output tokens", u.output_tokens),
            ("Cache read tokens", u.cache_read_input_tokens),
            ("Cache write tokens", u.cache_creation_input_tokens),
        ]
    rows.append(("Generated", datetime.now().strftime("%Y-%m-%d %H:%M")))
    return rows


def _summary_sheet(wb: Workbook, analysis: Analysis, recording: Recording, out_dir: Optional[Path] = None) -> Worksheet:
    return write_sheet(wb, "Summary", ["Item", "Value"], [list(r) for r in summary_rows(analysis, recording, out_dir)], recording)


# ------------------------------------------------------------------ the report


def export_markdown(analysis: Analysis, recording: Recording, out_dir: Path, filename: str = "report.md") -> Path:
    """Write the analysis as a readable Markdown report in out_dir and return its path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    names = screen_names(analysis)
    criteria = criteria_by_requirement(analysis)
    checks = lint_analysis(analysis)
    fields_by_screen = {s.id: [f for f in analysis.fields if f.screen_id == s.id] for s in analysis.screens}
    actions_by_screen = {s.id: [a for a in analysis.actions if a.screen_id == s.id] for s in analysis.screens}

    def link(index: int, timestamp: float) -> str:
        return frame_markdown(recording, index, timestamp)

    lines: list[str] = [f"# {analysis.title}", ""]

    lines += ["## Summary", "", analysis.summary, ""]
    for item, value in summary_rows(analysis, recording, out_dir):
        if item in ("Title", "Summary"):
            continue
        lines.append(f"- {item}: {value}")
    lines.append("")

    lines += ["## The journey, step by step", ""]
    for j in analysis.journey:
        who = f" ({j.actor})" if j.actor else ""
        lines.append(f"{j.order}. **{_name(names, j.screen_id)}**{who}: {j.description} {link(j.keyframe_index, j.timestamp)}")
    lines.append("")

    lines += ["## Screens", ""]
    for s in analysis.screens:
        first_index = s.keyframe_indexes[0] if s.keyframe_indexes else -1
        lines += [f"### {s.id}: {s.name}", "", f"{s.purpose} {link(first_index, s.first_seen)}", ""]
        lines += ["**Data fields**", ""]
        screen_fields = fields_by_screen.get(s.id, [])
        if not screen_fields:
            lines.append("- None recorded.")
        for f in screen_fields:
            extras = [f.field_type, f"required: {_required_text(f.required).lower()}", f.source]
            if f.example_value:
                extras.append(f"example: {f.example_value}")
            notes = f" {f.notes}" if f.notes else ""
            lines.append(f"- {f.id} {f.label} ({'; '.join(extras)}).{notes} {link(f.keyframe_index, f.timestamp)}")
        lines += ["", "**Actions**", ""]
        screen_actions = actions_by_screen.get(s.id, [])
        if not screen_actions:
            lines.append("- None recorded.")
        for a in screen_actions:
            control = f" [{a.control}]" if a.control else ""
            leads = f" Leads to {_name(names, a.leads_to_screen_id)}." if a.leads_to_screen_id else ""
            lines.append(f"- {a.id} {a.description}{control}.{leads} {link(a.keyframe_index, a.timestamp)}")
        lines.append("")

    lines += ["## Requirements", ""]
    for r in analysis.requirements:
        lines += [f"### {r.id}: {r.statement}", ""]
        details = f"{r.kind}, priority {r.priority}, confidence {r.confidence}"
        if r.screen_id:
            details += f", screen {_name(names, r.screen_id)}"
        lines.append(f"- {details}. {link(r.keyframe_index, r.timestamp)}")
        if r.rationale:
            lines.append(f"- Why: {r.rationale}")
        lines.append(f'- The expert said: "{r.source_quote}"')
        if checks.get(r.id):
            lines.append(f"- *Writing check: {format_findings(checks[r.id])}*")
        lines += ["", "Acceptance criteria:", ""]
        acs = criteria.get(r.id, [])
        if not acs:
            lines.append("- None yet.")
        for ac in acs:
            lines.append(f"- {ac.id}: Given {ac.given}, when {ac.when}, then {ac.then}. {link(ac.keyframe_index, ac.timestamp)}")
            if checks.get(ac.id):
                lines.append(f"  - *Writing check: {format_findings(checks[ac.id])}*")
        lines.append("")

    lines += ["## Questions for the expert", ""]
    for q in analysis.questions:
        lines += [f"### {q.id}: {q.question}", ""]
        where = f" (screen {_name(names, q.screen_id)})" if q.screen_id else ""
        lines.append(f"- Why it matters: {q.why_it_matters}")
        lines.append(f"- Category: {q.category}{where}. {link(q.keyframe_index, q.timestamp)}")
        if q.context_quote:
            lines.append(f'- What was said: "{q.context_quote}"')
        lines.append("")

    from .glossary import glossary_markdown, glossary_with_findings

    from .pii import load_ocr_text, pii_summary, scan_recording

    hits = scan_recording(recording, analysis, load_ocr_text(out_dir))
    lines += ["## Personal data seen", "", pii_summary(hits), ""]
    if hits:
        lines += ["| Kind | Value (masked) | Where | Time | Frame |", "|---|---|---|---|---|"]
        for hit in hits:
            frame = frame_markdown(recording, hit.keyframe_index, hit.timestamp or 0.0) if hit.keyframe_index is not None else ""
            lines.append(f"| {hit.kind} | {hit.value_masked} | {hit.source} | {mmss(hit.timestamp or 0.0)} | {frame} |")
        lines.append("")

    entries, findings = glossary_with_findings(analysis)
    lines += [glossary_markdown(entries), ""]
    if findings:
        lines += ["Naming to check:", ""] + [f"- {finding}" for finding in findings] + [""]

    lines += ["## What was said", ""]
    for seg in recording.segments:
        speaker = f"**{seg.speaker}:** " if seg.speaker else ""
        ref = frame_for_segment(recording, seg)
        tail = f" {link(ref.index, ref.timestamp)}" if ref else ""
        lines.append(f"- {mmss(seg.start)} {speaker}{seg.text}{tail}")
    lines.append("")

    path = out_dir / filename
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ------------------------------------------------------------------ both


def export_all(analysis: Analysis, recording: Recording, out_dir: Path) -> dict[str, Path]:
    """Write every output: the workbook, the Markdown report, the self-contained
    HTML report, and the Jira and Azure DevOps import files. Returns a name -> path dict."""
    from .html_report import export_html
    from .tickets import export_tickets

    paths = {
        "xlsx": export_xlsx(analysis, recording, out_dir),
        "markdown": export_markdown(analysis, recording, out_dir),
        "html": export_html(analysis, recording, out_dir),
    }
    paths.update(export_tickets(analysis, recording, out_dir))
    return paths
