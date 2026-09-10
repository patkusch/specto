"""Score an analysis against a hand-written answer key.

    specto score out/walkthrough expected.json --min 0.7

The answer key (`expected.json`) says what a good analysis must contain. It uses
case-insensitive substring matching so wording differences do not matter:

    {
      "screens": ["customer search", "customer details"],
      "fields": [{"screen": "customer details", "label": "postcode"}],
      "actions": [["submit", "approval"]],
      "requirements": [["postcode", "uk|format|valid"], ["team lead", "approve"]],
      "questions": [["document", "check|review|who"]]
    }

A "group" is a string such as "required|mandatory|must": any one of the
alternatives counts. An expected item is a list of groups and matches an
analysis item when every group has at least one alternative present in the
item's text. Screens match on the name, fields on the label (and the field's
screen must be one whose name contains the expected screen), actions on the
description, requirements on statement + rationale + source quote, questions on
question + why it matters + context quote.

Each analysis item can satisfy at most one expected item and each expected item
is satisfied by at most one analysis item, greedily in answer-key order.

Separately from the answer key, the scorer checks traceability: every
requirement has a source quote, every acceptance criterion points at a
requirement that exists, and no frame reference is negative.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from .model import Analysis

CATEGORIES = ("screens", "fields", "actions", "requirements", "questions")


# ------------------------------------------------------------------ report shape


class CategoryScore(BaseModel):
    expected: int = 0
    found: int = 0
    recall: float = Field(default=0.0, description="found / expected; 0 when nothing was expected")
    missed: list[Any] = Field(default_factory=list, description="Expected items not found, as written in the answer key")
    extras: int = Field(default=0, description="Analysis items that matched no expected item")


class Traceability(BaseModel):
    requirements_without_source_quote: int = 0
    criteria_with_unknown_requirement: int = 0
    negative_keyframe_indexes: int = 0

    @property
    def violations(self) -> int:
        return (self.requirements_without_source_quote
                + self.criteria_with_unknown_requirement
                + self.negative_keyframe_indexes)


class ScoreReport(BaseModel):
    categories: dict[str, CategoryScore]
    overall_recall: float = Field(description="Mean recall over categories with at least one expected item")
    traceability: Traceability


# --------------------------------------------------------------------- matching


def _norm_groups(spec: Any) -> list[str]:
    """An expected item is a list of groups; a bare string is one group."""
    if isinstance(spec, str):
        return [spec]
    if isinstance(spec, list) and all(isinstance(g, str) for g in spec):
        return list(spec)
    raise ValueError(f"expected item must be a string or a list of strings, got {spec!r}")


def _group_hit(text: str, group: str) -> bool:
    alternatives = [alt.strip().lower() for alt in group.split("|") if alt.strip()]
    return any(alt in text for alt in alternatives)


def matches(text: str, groups: list[str]) -> bool:
    """True when every group has at least one alternative present in `text` (case-insensitive)."""
    lowered = text.lower()
    return all(_group_hit(lowered, g) for g in groups)


def _join(*parts: str | None) -> str:
    return " ".join(p for p in parts if p)


def _field_matcher(analysis: Analysis) -> Callable[[Any, Any], bool]:
    screen_names = {s.id: s.name for s in analysis.screens}

    def match(field, spec) -> bool:
        if isinstance(spec, str):
            spec = {"label": spec}
        if not isinstance(spec, dict) or "label" not in spec:
            raise ValueError(f"expected field must be {{'screen': ..., 'label': ...}}, got {spec!r}")
        if not matches(field.label, _norm_groups(spec["label"])):
            return False
        wanted_screen = spec.get("screen")
        if wanted_screen:
            return matches(screen_names.get(field.screen_id, ""), _norm_groups(wanted_screen))
        return True

    return match


def _text_matcher(text_of: Callable[[Any], str]) -> Callable[[Any, Any], bool]:
    def match(item, spec) -> bool:
        return matches(text_of(item), _norm_groups(spec))

    return match


def _score_category(items: list, specs: list, match: Callable[[Any, Any], bool]) -> CategoryScore:
    unused = list(range(len(items)))
    missed: list[Any] = []
    for spec in specs:
        hit = next((i for i in unused if match(items[i], spec)), None)
        if hit is None:
            missed.append(spec)
        else:
            unused.remove(hit)
    expected = len(specs)
    found = expected - len(missed)
    return CategoryScore(
        expected=expected,
        found=found,
        recall=(found / expected) if expected else 0.0,
        missed=missed,
        extras=len(unused),
    )


def check_traceability(analysis: Analysis) -> Traceability:
    requirement_ids = {r.id for r in analysis.requirements}
    negative = sum(1 for s in analysis.screens for k in s.keyframe_indexes if k < 0)
    for group in (analysis.fields, analysis.actions, analysis.journey, analysis.requirements,
                  analysis.acceptance_criteria, analysis.questions):
        negative += sum(1 for item in group if item.keyframe_index < 0)
    return Traceability(
        requirements_without_source_quote=sum(1 for r in analysis.requirements if not r.source_quote.strip()),
        criteria_with_unknown_requirement=sum(1 for c in analysis.acceptance_criteria
                                              if c.requirement_id not in requirement_ids),
        negative_keyframe_indexes=negative,
    )


def score(analysis: Analysis, expected: dict) -> ScoreReport:
    unknown = {k for k in expected if not k.startswith('_')} - set(CATEGORIES)
    if unknown:
        raise ValueError(f"unknown categories in answer key: {sorted(unknown)}; "
                         f"use {', '.join(CATEGORIES)}")
    matchers = {
        "screens": (analysis.screens, _text_matcher(lambda s: s.name)),
        "fields": (analysis.fields, _field_matcher(analysis)),
        "actions": (analysis.actions, _text_matcher(lambda a: a.description)),
        "requirements": (analysis.requirements,
                         _text_matcher(lambda r: _join(r.statement, r.rationale, r.source_quote))),
        "questions": (analysis.questions,
                      _text_matcher(lambda q: _join(q.question, q.why_it_matters, q.context_quote))),
    }
    categories = {
        name: _score_category(items, list(expected.get(name) or []), match)
        for name, (items, match) in matchers.items()
    }
    scored = [c.recall for c in categories.values() if c.expected > 0]
    overall = sum(scored) / len(scored) if scored else 0.0
    return ScoreReport(categories=categories, overall_recall=overall, traceability=check_traceability(analysis))


# -------------------------------------------------------------------- reporting


def _spec_text(spec: Any) -> str:
    return spec if isinstance(spec, str) else json.dumps(spec)


def format_report(report: ScoreReport) -> str:
    lines = [f"{'Category':<14}{'Expected':>10}{'Found':>8}{'Recall':>8}{'Extras':>8}"]
    for name, c in report.categories.items():
        lines.append(f"{name:<14}{c.expected:>10}{c.found:>8}{c.recall:>8.2f}{c.extras:>8}")
    lines.append("Missed:")
    missed_any = False
    for name, c in report.categories.items():
        for spec in c.missed:
            lines.append(f"  {name}: {_spec_text(spec)}")
            missed_any = True
    if not missed_any:
        lines.append("  (none)")
    t = report.traceability
    lines.append("Traceability:")
    lines.append(f"  requirements without source quote: {t.requirements_without_source_quote}")
    lines.append(f"  acceptance criteria with unknown requirement: {t.criteria_with_unknown_requirement}")
    lines.append(f"  negative keyframe indexes: {t.negative_keyframe_indexes}")
    lines.append(f"Overall recall: {report.overall_recall:.2f}")
    return "\n".join(lines)


def score_dir(out_dir: str | Path, expected_path: str | Path) -> ScoreReport:
    """Score `out_dir/analysis.json` against the answer key and write `out_dir/score.json`."""
    out_dir = Path(out_dir)
    analysis = Analysis.model_validate_json((out_dir / "analysis.json").read_text(encoding="utf-8"))
    expected = json.loads(Path(expected_path).read_text(encoding="utf-8"))
    report = score(analysis, expected)
    (out_dir / "score.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="specto score",
                                     description="Compare an output folder with a hand-written answer key.")
    parser.add_argument("out_dir", help="output folder containing analysis.json")
    parser.add_argument("expected", help="answer key (expected.json)")
    parser.add_argument("--min", type=float, default=0.7,
                        help="exit 1 when overall recall is below this (default 0.7)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = score_dir(args.out_dir, args.expected)
    print(format_report(report))
    if report.overall_recall < args.min:
        print(f"FAIL: overall recall {report.overall_recall:.2f} is below --min {args.min:.2f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
