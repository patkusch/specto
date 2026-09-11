"""Write the requirements and questions as files a delivery team can import into Jira or Azure DevOps.

Both trackers import CSV files natively and map columns by header name, so no
library and no API key is needed. One row per requirement (a story with its
acceptance criteria) and one row per SME question (a task or issue), each
pointing back at the expert's words, the time and the still frame.

Helpers such as `screen_names` are repeated here rather than imported from
`export.py`, so `export.py` can import this module without a cycle.
"""
from __future__ import annotations

import csv
import html
import re
from pathlib import Path
from typing import Optional

from specto.model import AcceptanceCriterion, Analysis, Question, Recording, Requirement
from specto.timefmt import mmss

JIRA_HEADERS = ["Issue Type", "Summary", "Description", "Priority", "Labels", "Issue ID", "Parent ID"]
ADO_HEADERS = ["Work Item Type", "Title", "Description", "Acceptance Criteria", "Priority", "Tags"]

JIRA_PRIORITY = {"must": "Highest", "should": "High", "could": "Medium", "unknown": "Medium"}
ADO_PRIORITY = {"must": "1", "should": "2", "could": "3", "unknown": "3"}

SUMMARY_MAX = 250
LABEL_PREFIX = "specto"


# ------------------------------------------------------------------ helpers


def slugify(text: str) -> str:
    """Lowercase, letters and digits only, words joined by hyphens: 'Customer search' -> 'customer-search'."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words)


def trim(text: str, limit: int = SUMMARY_MAX) -> str:
    """Cut text to `limit` characters, ending with an ellipsis when something was dropped."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _frame_name(recording: Recording, index: int) -> str:
    """The file name of a keyframe, e.g. frame_0003.jpg, or 'frame 3' when unknown."""
    for kf in recording.keyframes:
        if kf.index == index:
            return Path(kf.path).name
    return f"frame {index}"


def _screen_names(analysis: Analysis) -> dict[str, str]:
    return {s.id: s.name for s in analysis.screens}


def _criteria_by_requirement(analysis: Analysis) -> dict[str, list[AcceptanceCriterion]]:
    grouped: dict[str, list[AcceptanceCriterion]] = {}
    for ac in analysis.acceptance_criteria:
        grouped.setdefault(ac.requirement_id, []).append(ac)
    return grouped


def _write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> Path:
    """Plain UTF-8, no BOM, every field quoted, LF line endings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)
    return path


# ------------------------------------------------------------------ Jira


def _jira_requirement_description(r: Requirement, criteria: list[AcceptanceCriterion], recording: Recording) -> str:
    lines: list[str] = []
    if r.rationale:
        lines += [r.rationale, ""]
    lines.append("h3. Acceptance criteria")
    if criteria:
        for ac in criteria:
            lines.append(f"* *Given* {ac.given} *When* {ac.when} *Then* {ac.then}")
    else:
        lines.append("* None yet.")
    lines += [
        "",
        "h3. Source",
        f'The expert said: "{r.source_quote}"',
        f"Time: {mmss(r.timestamp)}",
        f"Frame: {_frame_name(recording, r.keyframe_index)}",
    ]
    return "\n".join(lines)


def _jira_question_description(q: Question, recording: Recording) -> str:
    lines = [f"Why it matters: {q.why_it_matters}"]
    if q.context_quote:
        lines.append(f'What was said: "{q.context_quote}"')
    lines.append(f"Time: {mmss(q.timestamp)}")
    lines.append(f"Frame: {_frame_name(recording, q.keyframe_index)}")
    return "\n".join(lines)


def _labels(*parts: Optional[str]) -> str:
    """Space-separated Jira labels; blanks dropped, spaces inside a label turned into hyphens."""
    labels = [LABEL_PREFIX]
    for part in parts:
        if part:
            slug = slugify(part)
            if slug and slug not in labels:
                labels.append(slug)
    return " ".join(labels)


def export_jira_csv(analysis: Analysis, recording: Recording, out_dir: Path, filename: str = "jira_import.csv") -> Path:
    """One Story per requirement and one Task per question, in Jira's CSV import layout."""
    names = _screen_names(analysis)
    criteria = _criteria_by_requirement(analysis)
    rows: list[list[str]] = []
    for r in analysis.requirements:
        screen = names.get(r.screen_id, r.screen_id) if r.screen_id else None
        rows.append([
            "Story",
            trim(r.statement),
            _jira_requirement_description(r, criteria.get(r.id, []), recording),
            JIRA_PRIORITY.get(r.priority, "Medium"),
            _labels(r.kind, screen),
            r.id,
            "",
        ])
    for q in analysis.questions:
        screen = names.get(q.screen_id, q.screen_id) if q.screen_id else None
        rows.append([
            "Task",
            trim(f"Question: {q.question}"),
            _jira_question_description(q, recording),
            "Medium",
            _labels("question", q.category, screen),
            q.id,
            "",
        ])
    return _write_csv(Path(out_dir) / filename, JIRA_HEADERS, rows)


# ------------------------------------------------------------------ Azure DevOps


def _esc(text: str) -> str:
    """Escape the three characters HTML needs; quotes stay as typed so the text reads naturally."""
    return html.escape(text, quote=False)


def _p(text: str) -> str:
    return f"<p>{_esc(text)}</p>"


def _ado_requirement_description(r: Requirement, recording: Recording) -> str:
    parts: list[str] = []
    if r.rationale:
        parts.append(_p(r.rationale))
    parts.append(_p(f'The expert said: "{r.source_quote}"'))
    parts.append(_p(f"Time {mmss(r.timestamp)}, frame {_frame_name(recording, r.keyframe_index)}"))
    return "".join(parts)


def _ado_criteria(criteria: list[AcceptanceCriterion]) -> str:
    if not criteria:
        return _p("None yet.")
    items = "".join(
        f"<li><b>Given</b> {_esc(ac.given)} <b>When</b> {_esc(ac.when)} <b>Then</b> {_esc(ac.then)}</li>"
        for ac in criteria
    )
    return f"<ul>{items}</ul>"


def _ado_question_description(q: Question, recording: Recording) -> str:
    parts = [_p(f"Why it matters: {q.why_it_matters}")]
    if q.context_quote:
        parts.append(_p(f'What was said: "{q.context_quote}"'))
    parts.append(_p(f"Time {mmss(q.timestamp)}, frame {_frame_name(recording, q.keyframe_index)}"))
    return "".join(parts)


def _tags(*parts: Optional[str]) -> str:
    """Azure DevOps tags, separated by '; '."""
    tags = [LABEL_PREFIX]
    for part in parts:
        if part:
            slug = slugify(part)
            if slug and slug not in tags:
                tags.append(slug)
    return "; ".join(tags)


def export_ado_csv(analysis: Analysis, recording: Recording, out_dir: Path, filename: str = "azure_devops_import.csv") -> Path:
    """One User Story per requirement and one Issue per question, in Azure DevOps Boards' CSV import layout."""
    names = _screen_names(analysis)
    criteria = _criteria_by_requirement(analysis)
    rows: list[list[str]] = []
    for r in analysis.requirements:
        screen = names.get(r.screen_id, r.screen_id) if r.screen_id else None
        rows.append([
            "User Story",
            trim(r.statement),
            _ado_requirement_description(r, recording),
            _ado_criteria(criteria.get(r.id, [])),
            ADO_PRIORITY.get(r.priority, "3"),
            _tags(r.kind, screen),
        ])
    for q in analysis.questions:
        screen = names.get(q.screen_id, q.screen_id) if q.screen_id else None
        rows.append([
            "Issue",
            trim(f"Question: {q.question}"),
            _ado_question_description(q, recording),
            "",
            "3",
            _tags("question", q.category, screen),
        ])
    return _write_csv(Path(out_dir) / filename, ADO_HEADERS, rows)


# ------------------------------------------------------------------ both


def export_tickets(analysis: Analysis, recording: Recording, out_dir: Path) -> dict[str, Path]:
    """Write both import files; return {"jira_csv": path, "ado_csv": path}."""
    return {
        "jira_csv": export_jira_csv(analysis, recording, out_dir),
        "ado_csv": export_ado_csv(analysis, recording, out_dir),
    }
