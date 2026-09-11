"""Writing check for requirements and acceptance criteria, in plain Python.

Every statement the model writes is checked against a short list of rules drawn
from the INCOSE Guide to Writing Requirements and the GOV.UK user story
guidance, so a reader sees at a glance which rows need a rewrite. Nothing here
calls a model: it is word lists and a few regular expressions, so it runs on
every export and in every test.

Two severities:

- "warn": the row probably needs a rewrite (two rules in one sentence, a vague
  word, an escape clause, an open-ended list, an empty clause, a "then" with
  no outcome, a sentence over 40 words).
- "info": a note worth a glance. The heuristic behind it cannot tell, for
  example, an "and" that joins two rules from the "and" in "date and time",
  or a design word the expert stated as a constraint from one the model added.

The word lists are module constants so they are easy to extend. Matching is
case-insensitive on whole words, so "some" does not fire on "someone".
"""
from __future__ import annotations

import re
from typing import Iterable, Literal

from pydantic import BaseModel

from specto.model import AcceptanceCriterion, Analysis, Requirement

Severity = Literal["warn", "info"]


class Finding(BaseModel):
    rule: str
    message: str
    severity: Severity


# ------------------------------------------------------------------ word lists

# Words that join two thoughts into one sentence. "and" is listed separately
# because it so often sits inside a noun phrase ("postcode and surname").
JOINERS = ("or", "unless", "as well as", "but", "then")
SOFT_JOINERS = ("and",)

VAGUE_WORDS = (
    "appropriate", "appropriately", "adequate", "adequately", "sufficient", "sufficiently",
    "reasonable", "reasonably", "some", "several", "many", "few", "various",
    "quickly", "quick", "fast", "slow", "slowly", "promptly", "soon", "timely",
    "easy", "easily", "simple", "simply", "user friendly", "user-friendly", "intuitive",
    "efficient", "efficiently", "effective", "effectively", "relevant", "suitable",
    "robust", "flexible", "seamless", "seamlessly", "optimal", "minimal", "normal", "usual",
    "good", "bad", "better", "best", "approximately", "roughly",
)

ESCAPE_CLAUSES = (
    "where possible", "where practicable", "if possible", "if necessary", "if needed",
    "as appropriate", "as required", "as needed", "if practicable", "to the extent",
    "where appropriate", "where necessary", "when appropriate", "if applicable", "where applicable",
)

OPEN_ENDED_LISTS = (
    "etc", "and so on", "and so forth", "and the like", "including but not limited to",
    "and more", "and others", "or similar",
)

# Words that describe how to build it rather than what it must do.
IMPLEMENTATION_WORDS = (
    "database", "api", "apis", "table", "tables", "endpoint", "endpoints", "sql", "json", "xml",
    "stored procedure", "microservice", "microservices", "css", "html", "javascript", "schema",
    "button colour", "button color", "cache", "cookie", "cookies", "server", "servers",
)

ABSOLUTES = ("always", "never", "all", "every", "100%", "everything", "nothing", "none")

# A "then" that says only this names no outcome a tester could see.
PLACEHOLDER_OUTCOMES = (
    "it works", "works", "it is correct", "correctly", "it works correctly", "success", "successful",
    "it succeeds", "it passes", "passes", "ok", "done", "it is done", "fine", "it is fine", "nothing", "no error",
)

# Words that make a "then" clause observable when the verb heuristic (a word
# ending in -s, -ed, -es or -ing) does not catch it.
OUTCOME_WORDS = frozenset({
    "is", "are", "has", "have", "can", "cannot", "must", "will", "does", "do", "show", "shows", "shown",
    "appear", "appears", "open", "opens", "display", "displays", "displayed", "return", "returns",
    "change", "changes", "become", "becomes", "remain", "remains", "contain", "contains", "list", "lists",
    "listed", "send", "sends", "sent", "receive", "receives", "save", "saves", "saved", "reject", "rejects",
    "get", "gets", "move", "moves", "stay", "stays", "match", "matches", "equal", "equals", "read", "reads",
    "exist", "exists", "seen", "hidden", "visible", "available", "present", "empty", "active", "disabled",
    "enabled", "blank", "unchanged", "gone", "written", "given", "taken", "chosen", "kept", "left",
})

# Words ending in -s that are not verbs; keeps the outcome heuristic honest.
NOT_VERBS = frozenset({"status", "address", "this", "its", "us", "plus", "yes", "class", "process", "access",
                       "success", "always", "less", "unless", "across", "business", "progress", "previous",
                       "various", "is", "has", "was", "does"})

# Words ending in -ed or -en that are not past participles.
NOT_PARTICIPLES = frozenset({"then", "when", "open", "often", "even", "seven", "eleven", "green", "between",
                             "need", "speed", "indeed", "screen", "teen", "keen", "shed", "red", "bed", "wed",
                             "feed", "seed", "deed", "heed", "weed", "bleed", "breed", "exceed", "proceed",
                             "succeed", "agreed", "freed", "kitchen", "chicken", "golden", "wooden", "sudden",
                             "hidden", "token", "oxygen", "citizen", "linen", "warden", "garden", "burden"})

# How a statement can start and be taken as naming its actor.
ACTOR_STARTS = ("as a ", "as an ", "the ")

# Modal and helper verbs; a capitalised first word followed by one of these
# within the next five words reads as "<actor> must ...".
MODALS = frozenset({"must", "shall", "should", "will", "would", "can", "could", "may", "might", "need", "needs",
                    "is", "are", "has", "have", "does", "do", "ensure", "ensures", "allow", "allows", "provide",
                    "provides", "support", "supports", "cannot"})

MAX_WORDS = 40

_PATTERNS: dict[str, re.Pattern] = {}


def _pattern(phrase: str) -> re.Pattern:
    """Whole-word, case-insensitive, any run of whitespace between the words of a phrase."""
    if phrase not in _PATTERNS:
        body = r"\s+".join(re.escape(part) for part in phrase.split())
        _PATTERNS[phrase] = re.compile(r"(?<![\w-])" + body + r"(?![\w-])", re.IGNORECASE)
    return _PATTERNS[phrase]


def _found(text: str, phrases: Iterable[str]) -> list[str]:
    """The phrases that appear in `text`, in the order they first appear, as written in the list."""
    hits: list[tuple[int, str]] = []
    for phrase in phrases:
        m = _pattern(phrase).search(text)
        if m:
            hits.append((m.start(), phrase))
    return [phrase for _, phrase in sorted(hits)]


_SO_THAT_TAIL = re.compile(r",?\s+so that\b.*$", re.IGNORECASE | re.DOTALL)
_PASSIVE = re.compile(r"\b(is|are|be|been|being|was|were)\s+(?:(?:not|also|only|then|\w+ly)\s+)?([A-Za-z]+(?:ed|en))\b",
                      re.IGNORECASE)


def _strip_benefit(statement: str) -> str:
    """Drop the 'so that ...' tail of a user story; the benefit clause may say anything."""
    return _SO_THAT_TAIL.sub("", statement)


def _where(clause: str | None) -> str:
    return f' in the "{clause}" part' if clause else ""


# ----------------------------------------------------------------- the checks


def _joined_clauses(text: str, and_severity: Severity, clause: str | None = None) -> list[Finding]:
    findings = []
    for word in _found(text, SOFT_JOINERS):
        findings.append(Finding(
            rule="joined-clauses", severity=and_severity,
            message=f'"{word}"{_where(clause)} may join two thoughts in one sentence, so split it if it does.'))
    for word in _found(text, JOINERS):
        findings.append(Finding(
            rule="joined-clauses", severity="warn",
            message=f'"{word}"{_where(clause)} joins two thoughts in one sentence, so write one sentence per thought.'))
    return findings


def _vague(text: str, clause: str | None = None) -> list[Finding]:
    return [Finding(rule="vague-word", severity="warn",
                    message=f'"{word}"{_where(clause)} is vague, so say what a tester could measure instead.')
            for word in _found(text, VAGUE_WORDS)]


def _escape(text: str, clause: str | None = None) -> list[Finding]:
    return [Finding(rule="escape-clause", severity="warn",
                    message=f'"{phrase}"{_where(clause)} lets the rule be skipped, so say exactly when it applies.')
            for phrase in _found(text, ESCAPE_CLAUSES)]


def _open_ended(text: str, clause: str | None = None) -> list[Finding]:
    return [Finding(rule="open-ended-list", severity="warn",
                    message=f'"{phrase}"{_where(clause)} leaves the list open, so name every item.')
            for phrase in _found(text, OPEN_ENDED_LISTS)]


def _implementation(text: str) -> list[Finding]:
    return [Finding(rule="implementation-word", severity="info",
                    message=f'"{word}" describes how to build it rather than what it must do.')
            for word in _found(text, IMPLEMENTATION_WORDS)]


def _absolutes(text: str) -> list[Finding]:
    return [Finding(rule="absolute", severity="info",
                    message=f'"{word}" is an absolute, so check it is really meant.')
            for word in _found(text, ABSOLUTES)]


def _has_actor(statement: str) -> bool:
    s = statement.strip()
    if s.lower().startswith(ACTOR_STARTS):
        return True
    words = re.findall(r"[A-Za-z][\w'-]*", s)
    if not words or not words[0][0].isupper() or words[0].lower() in MODALS:
        return False
    if any(w.lower() in MODALS for w in words[1:6]):
        return True
    second = words[1].lower() if len(words) > 1 else ""
    return len(second) > 3 and second.endswith("s") and second not in NOT_VERBS


def _missing_actor(statement: str) -> list[Finding]:
    if _has_actor(statement):
        return []
    return [Finding(rule="missing-actor", severity="info",
                    message='The statement does not say who or what does this, so start with the role or "The system".')]


def _passive(text: str) -> list[Finding]:
    for m in _PASSIVE.finditer(text):
        participle = m.group(2)
        if len(participle) >= 4 and participle.lower() not in NOT_PARTICIPLES:
            return [Finding(rule="passive-voice", severity="info",
                            message=f'"{m.group(0)}" is passive, so say who or what does it.')]
    return []


def _too_long(text: str, clause: str | None = None) -> list[Finding]:
    n = len(re.findall(r"\S+", text))
    if n <= MAX_WORDS:
        return []
    return [Finding(rule="too-long", severity="warn",
                    message=f'{n} words{_where(clause)} is over the {MAX_WORDS}-word limit, so split it into shorter sentences.')]


def _verb_like(word: str) -> bool:
    if word in OUTCOME_WORDS:
        return True
    if word in NOT_VERBS or len(word) < 4:
        return False
    return word.endswith(("ed", "es", "ing", "s"))


def _no_outcome(then: str) -> list[Finding]:
    text = then.strip().rstrip(".!").strip()
    if text.lower() in PLACEHOLDER_OUTCOMES:
        return [Finding(rule="no-outcome", severity="warn",
                        message=f'The "then" part says only "{text}", so name what a tester would see.')]
    words = re.findall(r"[a-z][a-z'-]*", text.lower())
    if not any(_verb_like(w) for w in words):
        return [Finding(rule="no-outcome", severity="warn",
                        message='The "then" part names no outcome a tester could see, so say what happens.')]
    return []


# ----------------------------------------------------------------- public API


def lint_requirement(req: Requirement) -> list[Finding]:
    """Findings on a requirement statement; empty when it is clean."""
    statement = req.statement.strip()
    if not statement:
        return [Finding(rule="empty-clause", severity="warn", message="The statement is empty.")]
    body = _strip_benefit(statement)
    findings: list[Finding] = []
    findings += _joined_clauses(body, "info")
    findings += _vague(statement)
    findings += _escape(statement)
    findings += _open_ended(statement)
    findings += _implementation(statement)
    findings += _missing_actor(statement)
    findings += _passive(statement)
    findings += _too_long(statement)
    findings += _absolutes(statement)
    return findings


def lint_criterion(ac: AcceptanceCriterion) -> list[Finding]:
    """Findings on a Given / When / Then criterion; empty when it is clean."""
    findings: list[Finding] = []
    parts = (("given", ac.given, "info"), ("when", ac.when, "warn"), ("then", ac.then, "info"))
    for name, text, and_severity in parts:
        if not text.strip():
            findings.append(Finding(rule="empty-clause", severity="warn", message=f'The "{name}" part is empty.'))
            continue
        findings += _joined_clauses(text, and_severity, name)
        findings += _vague(text, name)
        findings += _escape(text, name)
        findings += _open_ended(text, name)
        findings += _too_long(text, name)
    if ac.then.strip():
        findings += _no_outcome(ac.then)
    return findings


def lint_analysis(analysis: Analysis) -> dict[str, list[Finding]]:
    """Findings for every requirement and criterion, keyed by item id; clean items map to an empty list."""
    out: dict[str, list[Finding]] = {}
    for r in analysis.requirements:
        out[r.id] = lint_requirement(r)
    for ac in analysis.acceptance_criteria:
        out[ac.id] = lint_criterion(ac)
    return out


def is_criterion_id(item_id: str) -> bool:
    """Criteria ids start with AC (AC001); requirement ids with R (R001)."""
    return item_id.upper().startswith("AC")


def _count(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def summarize(findings_by_id: dict[str, list[Finding]]) -> str:
    """One line, e.g. '3 requirements and 2 criteria have warnings; 5 notes'."""
    req_warn = ac_warn = notes = 0
    for item_id, findings in findings_by_id.items():
        if any(f.severity == "warn" for f in findings):
            if is_criterion_id(item_id):
                ac_warn += 1
            else:
                req_warn += 1
        notes += sum(1 for f in findings if f.severity == "info")
    return (f"{_count(req_warn, 'requirement')} and {_count(ac_warn, 'criterion', 'criteria')} have warnings; "
            f"{_count(notes, 'note')}")


def format_findings(findings: list[Finding]) -> str:
    """The cell text: severity and message per finding, joined by '; '; empty when clean."""
    return "; ".join(f"{f.severity}: {f.message}" for f in findings)
