"""Glossary of domain terms, built from the analysis, with a naming check.

The INCOSE Guide to Writing Requirements asks that every term a requirement
set relies on is defined once and then used the same way everywhere. Nothing
here calls a model: the terms are lifted straight out of the analysis (the
actors, screen names, field labels, named buttons and status values), each
one is traced to the rows that mention it, and near-duplicate spellings such
as "Postcode" / "Post code" or "Team lead" / "Team leader" are pointed out so
the analyst can settle on one.

The "definition" column is left empty on purpose: it is the analyst's to fill
in, with the expert, once the list of terms is agreed.
"""
from __future__ import annotations

import re
from typing import Iterable, Literal, Optional

from pydantic import BaseModel, Field

from specto.model import Analysis
from specto.timefmt import mmss

Kind = Literal["role", "screen", "field", "action", "status value", "other"]

# The order the kinds appear in on the sheet: who, then where, then what.
KIND_ORDER: tuple[str, ...] = ("role", "screen", "field", "action", "status value", "other")

GLOSSARY_HEADERS = ["Term", "Kind", "Where", "First seen", "Frame", "Used in", "Definition", "Notes"]

# Verbs after which the rest of an action description names a control.
CONTROL_VERBS = ("presses", "press", "clicks", "click", "selects", "select", "opens", "open",
                 "taps", "tap", "chooses", "choose", "picks", "pick")

# Trailing words that describe the control type rather than its name.
CONTROL_TYPES = ("button", "link", "menu", "tab", "option", "icon")

# Words that read as a status value when they appear capitalised in a statement.
STATUS_WORDS = ("Pending", "Approved", "Rejected", "Verified", "Unverified", "Active", "Inactive",
                "Draft", "Submitted", "Completed", "Complete", "Cancelled", "Archived", "Declined",
                "Failed", "Passed", "Accepted", "Closed", "Suspended", "Expired")

# A term of fewer letters than this is not compared by edit distance: "Save"
# and "Fail" are two letters apart and nothing alike.
MIN_LETTERS_FOR_EDIT_DISTANCE = 6
MAX_EDIT_DISTANCE = 2


class GlossaryEntry(BaseModel):
    term: str
    kind: Kind
    first_seen: float = Field(description="Seconds from the start of the recording")
    keyframe_index: int
    where: str = Field(description="A short phrase: 'Customer details screen', 'listed as an actor'")
    used_in: list[str] = Field(default_factory=list, description="Ids of the rows that mention the term: R001, AC003, Q002, S02 ...")
    definition: str = Field(default="", description="Left empty; the analyst fills it in")
    notes: str = Field(default="", description="Empty unless the naming check found something")


# ------------------------------------------------------------------ matching


_PATTERNS: dict[tuple[str, bool], re.Pattern] = {}


def _pattern(term: str, plural: bool = True) -> re.Pattern:
    """Whole-word, case-insensitive match of a term, any whitespace or a hyphen
    between its words, and (unless plural is False) an optional plural on the last word."""
    key = (term, plural)
    if key not in _PATTERNS:
        parts = [re.escape(p) for p in re.split(r"[\s-]+", term.strip()) if p]
        body = r"[\s-]+".join(parts) + (r"(?:s|es)?" if plural else "")
        _PATTERNS[key] = re.compile(r"(?<![\w-])" + body + r"(?![\w-])", re.IGNORECASE)
    return _PATTERNS[key]


def _mentions(term: str, text: Optional[str], plural: bool = True) -> bool:
    return bool(text) and _pattern(term, plural).search(text) is not None


def _fold(term: str) -> str:
    """Lower case with spaces and hyphens removed, so 'Post code', 'post-code' and 'Postcode' agree."""
    return re.sub(r"[\s-]+", "", term).lower()


def _singular(folded: str) -> str:
    """A crude singular of a folded term, enough to pair 'Detail' with 'Details'."""
    if folded.endswith("ies") and len(folded) > 4:
        return folded[:-3] + "y"
    if folded.endswith(("ses", "xes", "shes", "ches")):
        return folded[:-2]
    if folded.endswith("s") and not folded.endswith("ss"):
        return folded[:-1]
    return folded


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance; small strings only, so the plain version is fine."""
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


# ------------------------------------------------------------------ term sources


_AS_A_ROLE = re.compile(r"^\s*as\s+an?\s+(.+?)\s*,", re.IGNORECASE)
_QUOTED = re.compile(r"[\"“”']([^\"“”']{2,})[\"“”']")
_AFTER_VERB = re.compile(r"\b(?:" + "|".join(CONTROL_VERBS) + r")\s+(?:the\s+|on\s+)?(.+)$", re.IGNORECASE)
_CUT_TAIL = re.compile(r"\s+(?:to|from|which|that|so|then|and then|in order)\b.*$", re.IGNORECASE)
_TYPE_TAIL = re.compile(r"\s+(?:" + "|".join(CONTROL_TYPES) + r")\s*$", re.IGNORECASE)


def role_in_statement(statement: str) -> Optional[str]:
    """The role named by 'As a <role>, ...' at the start of a statement, or None."""
    m = _AS_A_ROLE.match(statement)
    return m.group(1).strip() if m else None


def control_name(description: str) -> Optional[str]:
    """The named button or menu in an action description, or None.

    'Presses Save and continue' -> 'Save and continue'; 'Clicks the "Approve" button' -> 'Approve';
    'Uploads the ID scan' -> None (no control verb, nothing quoted).
    """
    text = description.strip().rstrip(".;,")
    m = _QUOTED.search(text)
    if m:
        return m.group(1).strip()
    m = _AFTER_VERB.search(text)
    if not m:
        return None
    name = _CUT_TAIL.sub("", m.group(1)).strip().rstrip(".;,")
    name = _TYPE_TAIL.sub("", name).strip()
    if not name or not name[0].isupper():
        return None
    return name


def _status_values_in(text: str) -> list[str]:
    """Capitalised status words in a sentence, as written."""
    found = []
    for word in STATUS_WORDS:
        if re.search(r"(?<![\w-])" + word + r"(?![\w-])", text):
            found.append(word)
    return found


# ------------------------------------------------------------------ building


def _searchable(analysis: Analysis) -> list[tuple[str, str]]:
    """Every (id, text) pair a term can be used in, in sheet order: screens, actions,
    requirements, criteria, questions."""
    out: list[tuple[str, str]] = []
    for s in analysis.screens:
        out.append((s.id, s.purpose))
    for a in analysis.actions:
        out.append((a.id, a.description))
    for r in analysis.requirements:
        out.append((r.id, " ".join(t for t in (r.statement, r.rationale, r.source_quote) if t)))
    for ac in analysis.acceptance_criteria:
        out.append((ac.id, " ".join((ac.given, ac.when, ac.then))))
    for q in analysis.questions:
        out.append((q.id, q.question))
    return out


def used_in(term: str, analysis: Analysis, plural: bool = True) -> list[str]:
    """Ids of the rows whose text mentions the term as a whole word, case-insensitive.

    A plural of the term counts too ("approvals queues"), unless plural is False;
    button names are matched exactly so "Approve" does not fire on "approves".
    """
    ids: list[str] = []
    for item_id, text in _searchable(analysis):
        if _mentions(term, text, plural) and item_id not in ids:
            ids.append(item_id)
    return ids


def _screen_names(analysis: Analysis) -> dict[str, str]:
    return {s.id: s.name for s in analysis.screens}


def _screen_phrase(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return f"{names[0]} screen"
    return f"{', '.join(names[:-1])} and {names[-1]} screens"


def _first_frame(analysis: Analysis) -> tuple[float, int]:
    if analysis.screens:
        s = min(analysis.screens, key=lambda s: s.first_seen)
        return s.first_seen, (s.keyframe_indexes[0] if s.keyframe_indexes else 0)
    return 0.0, 0


def _role_first_seen(role: str, analysis: Analysis) -> tuple[float, int]:
    """The earliest moment a role is named: a journey step with that actor, else the
    earliest row that mentions it, else the start of the recording."""
    candidates: list[tuple[float, int]] = []
    for j in analysis.journey:
        if j.actor and _fold(j.actor) == _fold(role):
            candidates.append((j.timestamp, j.keyframe_index))
    if not candidates:
        for r in analysis.requirements:
            if _mentions(role, r.statement) or _mentions(role, r.source_quote):
                candidates.append((r.timestamp, r.keyframe_index))
        for ac in analysis.acceptance_criteria:
            if any(_mentions(role, t) for t in (ac.given, ac.when, ac.then)):
                candidates.append((ac.timestamp, ac.keyframe_index))
        for q in analysis.questions:
            if _mentions(role, q.question):
                candidates.append((q.timestamp, q.keyframe_index))
    return min(candidates) if candidates else _first_frame(analysis)


def _roles(analysis: Analysis) -> list[GlossaryEntry]:
    entries: list[GlossaryEntry] = []
    seen: dict[str, GlossaryEntry] = {}
    for actor in analysis.actors:
        key = _fold(actor)
        if key in seen:
            continue
        ts, kf = _role_first_seen(actor, analysis)
        entry = GlossaryEntry(term=actor, kind="role", first_seen=ts, keyframe_index=kf,
                              where="listed as an actor", used_in=used_in(actor, analysis))
        seen[key] = entry
        entries.append(entry)
    for r in analysis.requirements:
        role = role_in_statement(r.statement)
        if not role or _fold(role) in seen:
            continue
        entry = GlossaryEntry(term=role, kind="role", first_seen=r.timestamp, keyframe_index=r.keyframe_index,
                              where=f"in requirement {r.id}", used_in=used_in(role, analysis))
        seen[_fold(role)] = entry
        entries.append(entry)
    return entries


def _screens(analysis: Analysis) -> list[GlossaryEntry]:
    return [
        GlossaryEntry(term=s.name, kind="screen", first_seen=s.first_seen,
                      keyframe_index=s.keyframe_indexes[0] if s.keyframe_indexes else 0,
                      where=f"screen {s.id}", used_in=used_in(s.name, analysis))
        for s in analysis.screens
    ]


def _fields(analysis: Analysis) -> list[GlossaryEntry]:
    """One entry per distinct label (case-insensitive), naming every screen it appears on."""
    names = _screen_names(analysis)
    groups: dict[str, list] = {}
    for f in analysis.fields:
        groups.setdefault(f.label.strip().lower(), []).append(f)
    entries = []
    for group in groups.values():
        first = min(group, key=lambda f: f.timestamp)
        screens: list[str] = []
        for f in group:
            name = names.get(f.screen_id, f.screen_id)
            if name not in screens:
                screens.append(name)
        entries.append(GlossaryEntry(term=first.label.strip(), kind="field", first_seen=first.timestamp,
                                     keyframe_index=first.keyframe_index, where=_screen_phrase(screens),
                                     used_in=used_in(first.label, analysis)))
    return entries


def _actions(analysis: Analysis) -> list[GlossaryEntry]:
    """One entry per named control, e.g. 'Save and continue' from 'Presses Save and continue'."""
    names = _screen_names(analysis)
    groups: dict[str, list[tuple[str, object]]] = {}
    for a in analysis.actions:
        name = control_name(a.description)
        if name:
            groups.setdefault(name.lower(), []).append((name, a))
    entries = []
    for group in groups.values():
        name, first = min(group, key=lambda pair: pair[1].timestamp)
        screens: list[str] = []
        for _, a in group:
            s = names.get(a.screen_id, a.screen_id)
            if s not in screens:
                screens.append(s)
        control = (first.control or "control").strip()
        entries.append(GlossaryEntry(term=name, kind="action", first_seen=first.timestamp,
                                     keyframe_index=first.keyframe_index,
                                     where=f"{control} on {_screen_phrase(screens)}",
                                     used_in=used_in(name, analysis, plural=False)))
    return entries


def _status_values(analysis: Analysis) -> list[GlossaryEntry]:
    """Values of status fields, plus capitalised status words in statements and criteria."""
    names = _screen_names(analysis)
    entries: list[GlossaryEntry] = []
    seen: dict[str, GlossaryEntry] = {}

    def add(term: str, ts: float, kf: int, where: str) -> None:
        key = _fold(term)
        if key in seen:
            return
        # "Pending" adds nothing when "Pending approval" is already listed.
        if any(_mentions(term, e.term) for e in seen.values()):
            return
        entry = GlossaryEntry(term=term, kind="status value", first_seen=ts, keyframe_index=kf,
                              where=where, used_in=used_in(term, analysis))
        seen[key] = entry
        entries.append(entry)

    for f in sorted(analysis.fields, key=lambda f: f.timestamp):
        if not f.example_value:
            continue
        if "status" in f.label.lower() or "status" in f.field_type.lower():
            add(f.example_value.strip(), f.timestamp, f.keyframe_index,
                f'value of the "{f.label}" field on {names.get(f.screen_id, f.screen_id)} screen')

    rows: list[tuple[float, int, str, str]] = []
    for r in analysis.requirements:
        rows.append((r.timestamp, r.keyframe_index, r.id, r.statement))
    for ac in analysis.acceptance_criteria:
        rows.append((ac.timestamp, ac.keyframe_index, ac.id, " ".join((ac.given, ac.when, ac.then))))
    for ts, kf, item_id, text in sorted(rows, key=lambda row: row[0]):
        for word in _status_values_in(text):
            add(word, ts, kf, f"in {item_id}")
    return entries


def _sort_key(entry: GlossaryEntry) -> tuple[int, str]:
    return (KIND_ORDER.index(entry.kind), entry.term.lower())


def build_glossary(analysis: Analysis) -> list[GlossaryEntry]:
    """Every domain term the analysis relies on, sorted by kind then term."""
    entries = _roles(analysis) + _screens(analysis) + _fields(analysis) + _actions(analysis) + _status_values(analysis)
    return sorted(entries, key=_sort_key)


# ------------------------------------------------------------------ the naming check


def _describe(entry: GlossaryEntry) -> str:
    return f'"{entry.term}" ({entry.kind}, {entry.where})'


def _how_they_differ(a: str, b: str, same_kind: bool) -> Optional[str]:
    """Why two different terms count as near-duplicates, or None if they do not."""
    if a.lower() == b.lower():
        return "differ only in capital letters"
    fa, fb = _fold(a), _fold(b)
    if fa == fb:
        return "differ only by a space or hyphen"
    if _singular(fa) == _singular(fb):
        return "differ only by a plural ending"
    if same_kind and min(len(fa), len(fb)) >= MIN_LETTERS_FOR_EDIT_DISTANCE and \
            _edit_distance(fa, fb) <= MAX_EDIT_DISTANCE:
        return "are one or two letters apart"
    return None


def _note(entry: GlossaryEntry, finding: str) -> None:
    if finding in entry.notes:
        return
    entry.notes = f"{entry.notes} {finding}".strip()


def _from_requirement(entry: GlossaryEntry) -> bool:
    """True for a role that only appears in an 'As a <role>' statement, not in the actors list."""
    return entry.kind == "role" and entry.where.startswith("in requirement")


def _near_duplicates(entries: list[GlossaryEntry]) -> list[str]:
    findings = []
    for i, a in enumerate(entries):
        for b in entries[i + 1:]:
            if a.term == b.term:
                continue
            # A role that is not an actor gets its own finding, naming the closest actor.
            if a.kind == "role" and b.kind == "role" and (_from_requirement(a) or _from_requirement(b)):
                continue
            reason = _how_they_differ(a.term, b.term, a.kind == b.kind)
            if reason is None:
                continue
            finding = f"{_describe(a)} and {_describe(b)} {reason}, so pick one spelling and use it everywhere."
            findings.append(finding)
            _note(a, finding)
            _note(b, finding)
    return findings


def _fields_on_several_screens(entries: list[GlossaryEntry], analysis: Analysis) -> list[str]:
    names = _screen_names(analysis)
    by_label: dict[str, list] = {}
    for f in analysis.fields:
        by_label.setdefault(_fold(f.label), []).append(f)
    findings = []
    for key, group in by_label.items():
        screens: list[str] = []
        for f in group:
            screen = names.get(f.screen_id, f.screen_id)
            spelled = f'{screen} as "{f.label}"' if f.label != group[0].label else screen
            if spelled not in screens:
                screens.append(spelled)
        if len(screens) < 2:
            continue
        finding = (f'Info: the field "{group[0].label}" appears on {len(screens)} screens ({", ".join(screens)}), '
                   f"so check it means the same thing on each.")
        findings.append(finding)
        for e in entries:
            if e.kind == "field" and _fold(e.term) == key:
                _note(e, finding)
    return findings


def _roles_not_in_actors(entries: list[GlossaryEntry], analysis: Analysis) -> list[str]:
    actors = {_fold(a): a for a in analysis.actors}
    findings = []
    for r in analysis.requirements:
        role = role_in_statement(r.statement)
        if not role or _fold(role) in actors:
            continue
        closest = _closest_actor(role, analysis.actors)
        hint = f' The closest listed actor is "{closest}".' if closest else ""
        listed = ", ".join(analysis.actors) if analysis.actors else "none listed"
        finding = (f'"{role}" is used as a role in {r.id} but is not in the actors list ({listed}), '
                   f"so add it or use the listed name.{hint}")
        findings.append(finding)
        for e in entries:
            if e.kind == "role" and _fold(e.term) == _fold(role):
                _note(e, finding)
    return findings


def _closest_actor(role: str, actors: Iterable[str]) -> Optional[str]:
    best: Optional[str] = None
    for actor in actors:
        if _how_they_differ(role, actor, True) is not None:
            return actor
        if _fold(actor) in _fold(role) or _fold(role) in _fold(actor):
            best = best or actor
    return best


def consistency_findings(entries: list[GlossaryEntry], analysis: Analysis) -> list[str]:
    """Plain sentences on naming problems, each also written into the notes of the
    entries it concerns: near-duplicate terms, a field label on more than one
    screen (info), and a role used in a requirement that is not an actor."""
    findings: list[str] = []
    findings += _near_duplicates(entries)
    findings += _fields_on_several_screens(entries, analysis)
    findings += _roles_not_in_actors(entries, analysis)
    return findings


def glossary_with_findings(analysis: Analysis) -> tuple[list[GlossaryEntry], list[str]]:
    """Build the glossary and run the naming check in one go."""
    entries = build_glossary(analysis)
    return entries, consistency_findings(entries, analysis)


# ------------------------------------------------------------------ output


def glossary_rows(entries: list[GlossaryEntry]) -> list[list]:
    """Header row plus one row per entry, ready for a 'Glossary' sheet.

    'First seen' is mm:ss and 'Frame' is the keyframe index; the exporter turns
    the index into a link to the still image.
    """
    rows: list[list] = [list(GLOSSARY_HEADERS)]
    for e in entries:
        rows.append([e.term, e.kind, e.where, mmss(e.first_seen), e.keyframe_index,
                     ", ".join(e.used_in), e.definition, e.notes])
    return rows


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def glossary_markdown(entries: list[GlossaryEntry]) -> str:
    """A '## Glossary' section with one table row per term."""
    rows = glossary_rows(entries)
    lines = ["## Glossary", ""]
    if not entries:
        lines += ["No terms found.", ""]
        return "\n".join(lines)
    lines.append("| " + " | ".join(rows[0]) + " |")
    lines.append("|" + "---|" * len(rows[0]))
    for row in rows[1:]:
        lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
    lines.append("")
    return "\n".join(lines)
