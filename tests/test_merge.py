"""Offline tests for merging two finished runs into one folder. No model call."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from PIL import Image

from specto.export import frame_for_segment
from specto.merge import merge_dirs, merge_report
from specto.model import Analysis, Recording

FIXTURES = Path(__file__).parent / "fixtures"


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def write_run(folder: Path, analysis: dict, recording: dict, ocr: bool = True) -> None:
    """A finished run on disk: the two JSON files, one JPEG per keyframe, and ocr.json."""
    (folder / "frames").mkdir(parents=True)
    for kf in recording["keyframes"]:
        Image.new("RGB", (32, 24), (kf["index"] * 30, 90, 160)).save(folder / kf["path"], "JPEG")
    (folder / "analysis.json").write_text(json.dumps(analysis))
    (folder / "recording.json").write_text(json.dumps(recording))
    if ocr:
        (folder / "ocr.json").write_text(json.dumps({str(kf["index"]): f"text {kf['index']}" for kf in recording["keyframes"]}))


def second_session(base: dict) -> dict:
    """A shorter second session over the same system.

    It sees the Customer details screen again (spelled differently), one new
    screen, repeats the email/age requirement (reflowed) with one criterion
    of its own, repeats the email question, and asks one new one.
    """
    a = copy.deepcopy(base)
    details = next(s for s in a["screens"] if s["id"] == "S02")
    details["name"] = "  customer DETAILS "
    details["keyframe_indexes"] = [0, 1]
    details["first_seen"] = 0.0
    details["field_ids"] = ["F007", "F011"]
    details["action_ids"] = ["A002"]
    address = {
        "id": "S05", "name": "Address history", "purpose": "List the customer's previous addresses.",
        "keyframe_indexes": [2, 3], "first_seen": 100.0, "field_ids": ["F012"], "action_ids": [],
    }
    a["screens"] = [details, address]

    email = next(f for f in a["fields"] if f["id"] == "F007")
    email.update({"label": "EMAIL", "timestamp": 5.0, "keyframe_index": 0})
    a["fields"] = [
        email,
        {"id": "F011", "screen_id": "S02", "label": "Mobile number", "field_type": "text", "required": None,
         "example_value": "07700 900123", "source": "seen on screen", "timestamp": 20.0, "keyframe_index": 0, "notes": None},
        {"id": "F012", "screen_id": "S05", "label": "Previous postcode", "field_type": "text", "required": None,
         "example_value": None, "source": "both", "timestamp": 110.0, "keyframe_index": 2, "notes": None},
    ]
    save = next(x for x in a["actions"] if x["id"] == "A002")
    save.update({"leads_to_screen_id": "S05", "timestamp": 90.0, "keyframe_index": 1})
    a["actions"] = [save]
    a["journey"] = [
        {"order": 1, "screen_id": "S02", "description": "The clerk adds the mobile number.", "actor": "Onboarding clerk",
         "timestamp": 20.0, "keyframe_index": 0},
        {"order": 2, "screen_id": "S05", "description": "The clerk checks the address history.", "actor": "Onboarding clerk",
         "timestamp": 110.0, "keyframe_index": 2},
    ]
    shared = next(r for r in a["requirements"] if r["id"] == "R002")
    shared["statement"] = "  the system MUST reject a customer record when the email address is not a valid format   or the date of birth shows the customer is under 18. "
    shared.update({"timestamp": 92.0, "keyframe_index": 1})
    a["requirements"] = [
        shared,
        {"id": "R005", "statement": "The system must keep every previous address of the customer.", "rationale": None,
         "source_quote": "We can see where they lived before.", "timestamp": 112.0, "keyframe_index": 2,
         "screen_id": "S05", "kind": "data", "priority": "should", "confidence": "medium"},
    ]
    a["acceptance_criteria"] = [
        {"id": "AC008", "requirement_id": "R002", "given": "the email field is blank", "when": "the clerk presses Save",
         "then": "the record is saved and no error is shown", "timestamp": 95.0, "keyframe_index": 1},
        {"id": "AC009", "requirement_id": "R005", "given": "a customer has moved twice", "when": "the clerk opens address history",
         "then": "three addresses are listed, newest first", "timestamp": 115.0, "keyframe_index": 2},
    ]
    shared_q = next(q for q in a["questions"] if q["id"] == "Q002")
    shared_q["question"] = "Is the email address mandatory, or can a customer be onboarded   without one?"
    shared_q.update({"timestamp": 30.0, "keyframe_index": 0})
    a["questions"] = [
        shared_q,
        {"id": "Q005", "question": "How far back does the address history go?", "why_it_matters": "Decides how much data to migrate.",
         "context_quote": "We can see where they lived before.", "timestamp": 118.0, "keyframe_index": 2,
         "screen_id": "S05", "category": "data"},
    ]
    a["title"] = "Second session"
    a["summary"] = "A second walk through the details and address screens."
    a["actors"] = ["onboarding clerk", "Compliance officer"]
    return a


def second_recording(base: dict) -> dict:
    r = copy.deepcopy(base)
    r["source"] = "second.mp4"
    r["duration"] = 150.0
    r["keyframes"] = r["keyframes"][:4]
    r["segments"] = [s for s in r["segments"] if s["start"] < 150.0]
    r["moments"] = r["moments"][:4]
    r["moments"][3]["end"] = 150.0
    return r


def build_sources(tmp_path: Path, second_ocr: bool = True) -> tuple[Path, Path]:
    analysis = json.loads((FIXTURES / "sample_analysis.json").read_text())
    recording = json.loads((FIXTURES / "sample_recording.json").read_text())
    a, b = tmp_path / "run_a", tmp_path / "run_b"
    write_run(a, analysis, recording)
    Image.new("RGB", (16, 12), (0, 0, 0)).save(a / "frames" / "crop_0001.jpg", "JPEG")
    write_run(b, second_session(analysis), second_recording(recording), ocr=second_ocr)
    return a, b


def test_merge_two_runs(tmp_path: Path) -> None:
    a, b = build_sources(tmp_path)
    out = tmp_path / "merged"
    logs: list[str] = []

    analysis, recording = merge_dirs([a, b], out, log=logs.append)

    # Recording: played back to back.
    assert len(recording.keyframes) == 6 + 4
    assert [k.index for k in recording.keyframes] == list(range(10))
    assert recording.keyframes[6].timestamp == 300.0
    assert recording.keyframes[7].timestamp == 345.0
    assert recording.duration == 450.0
    assert recording.source == "merged: onboarding_walkthrough.mp4 + second.mp4"
    assert recording.transcript_source == "file"
    for k in recording.keyframes:
        assert k.path == f"frames/frame_{k.index:04d}.jpg"
        assert (out / k.path).exists()
    assert (out / "frames" / "crop_0001.jpg").exists()
    assert not (out / "frames" / "crop_0007.jpg").exists()
    assert len(recording.segments) == 12 + 7
    assert recording.segments[12].start == 302.0
    # Moments cover every segment and every frame.
    assert [m.keyframe_index for m in recording.moments] == list(range(10))
    assert recording.moments[6].start == 300.0 and recording.moments[-1].end == 450.0
    for segment in recording.segments:
        assert frame_for_segment(recording, segment) is not None
    ocr = json.loads((out / "ocr.json").read_text())
    assert sorted(int(k) for k in ocr) == list(range(10))
    assert ocr["6"] == "text 0" and ocr["9"] == "text 3"

    # Analysis: the shared screen appears once, with frames from both runs.
    details = [s for s in analysis.screens if _norm(s.name) == "customer details"]
    assert len(details) == 1
    assert details[0].keyframe_indexes == [1, 2, 6, 7]
    assert details[0].first_seen == 45.0
    assert details[0].name == "Customer details"
    names = [s.name for s in analysis.screens]
    assert "Address history" in names
    assert len(analysis.screens) == 5

    # Fields: Email merged (earliest kept), the new ones present.
    emails = [f for f in analysis.fields if _norm(f.label) == "email"]
    assert len(emails) == 1 and emails[0].timestamp == 58.0 and emails[0].screen_id == details[0].id
    assert len(analysis.fields) == 10 + 2
    labels = {f.label for f in analysis.fields}
    assert {"Mobile number", "Previous postcode"} <= labels
    assert set(details[0].field_ids) == {f.id for f in analysis.fields if f.screen_id == details[0].id}
    assert len(details[0].field_ids) == 6

    # The shared requirement appears once; criteria from both runs point at it.
    shared = [r for r in analysis.requirements if _norm(r.statement).startswith("the system must reject a customer record")]
    assert len(shared) == 1
    assert shared[0].timestamp == 125.0
    assert len(analysis.requirements) == 4 + 1
    criteria = [c for c in analysis.acceptance_criteria if c.requirement_id == shared[0].id]
    assert len(criteria) == 3
    assert {c.keyframe_index for c in criteria} == {2, 7}
    assert len(analysis.acceptance_criteria) == 7 + 2

    # Questions: the repeated one once, the new one present.
    assert len(analysis.questions) == 4 + 1
    assert sum(1 for q in analysis.questions if _norm(q.question).startswith("is the email address mandatory")) == 1
    assert any(q.question.startswith("How far back") for q in analysis.questions)

    # Journey, actors, title, summary, usage.
    assert [j.order for j in analysis.journey] == list(range(1, 8))
    assert analysis.journey[5].timestamp == 320.0
    assert analysis.actors == ["Onboarding clerk", "Team manager", "Compliance officer"]
    assert analysis.title == "Customer onboarding walkthrough (merged from 2 sessions)"
    assert analysis.summary.endswith("\n\nA second walk through the details and address screens.")
    assert analysis.usage is not None
    assert analysis.usage.calls == 6 and analysis.usage.input_tokens == 2 * 48210

    # Ids are clean and every cross-reference resolves.
    assert [s.id for s in analysis.screens] == [f"S{n:02d}" for n in range(1, 6)]
    assert [f.id for f in analysis.fields] == [f"F{n:03d}" for n in range(1, 13)]
    assert [x.id for x in analysis.actions] == [f"A{n:03d}" for n in range(1, 7)]
    assert [r.id for r in analysis.requirements] == [f"R{n:03d}" for n in range(1, 6)]
    assert [c.id for c in analysis.acceptance_criteria] == [f"AC{n:03d}" for n in range(1, 10)]
    assert [q.id for q in analysis.questions] == [f"Q{n:03d}" for n in range(1, 6)]
    screen_ids = {s.id for s in analysis.screens}
    requirement_ids = {r.id for r in analysis.requirements}
    field_ids = {f.id for f in analysis.fields}
    action_ids = {x.id for x in analysis.actions}
    keyframe_indexes = {k.index for k in recording.keyframes}
    for field in analysis.fields:
        assert field.screen_id in screen_ids
        assert field.keyframe_index in keyframe_indexes
    for action in analysis.actions:
        assert action.screen_id in screen_ids
        assert action.leads_to_screen_id is None or action.leads_to_screen_id in screen_ids
        assert action.keyframe_index in keyframe_indexes
    for step in analysis.journey:
        assert step.screen_id in screen_ids
        assert step.keyframe_index in keyframe_indexes
    for requirement in analysis.requirements:
        assert requirement.screen_id is None or requirement.screen_id in screen_ids
        assert requirement.keyframe_index in keyframe_indexes
    for criterion in analysis.acceptance_criteria:
        assert criterion.requirement_id in requirement_ids
        assert criterion.keyframe_index in keyframe_indexes
    for question in analysis.questions:
        assert question.screen_id is None or question.screen_id in screen_ids
        assert question.keyframe_index in keyframe_indexes
    for screen in analysis.screens:
        assert set(screen.field_ids) <= field_ids
        assert set(screen.action_ids) <= action_ids
        assert set(screen.keyframe_indexes) <= keyframe_indexes
    # The second run's Save now leads to the Address history screen.
    address = next(s for s in analysis.screens if s.name == "Address history")
    second_save = [x for x in analysis.actions if x.keyframe_index == 7]
    assert second_save and second_save[0].leads_to_screen_id == address.id

    # Files on disk reload to the same thing.
    assert Analysis.model_validate_json((out / "analysis.json").read_text()) == analysis
    assert Recording.model_validate_json((out / "recording.json").read_text()) == recording
    assert any("merged 2 runs" in line for line in logs)

    report = merge_report([a, b], analysis)
    print(report)
    assert str(a) in report and str(b) in report
    assert "4 screens" in report and "2 screens" in report
    assert "screens: 6 came in, 5 kept (1 folded into an earlier one)" in report
    assert "requirements: 6 came in, 5 kept (1 folded into an earlier one)" in report
    assert "acceptance criteria: 9 came in, 9 kept" in report


def test_merge_skips_ocr_when_a_run_has_none(tmp_path: Path) -> None:
    a, b = build_sources(tmp_path, second_ocr=False)
    out = tmp_path / "merged"
    logs: list[str] = []
    merge_dirs([a, b], out, log=logs.append)
    assert not (out / "ocr.json").exists()
    assert any("ocr.json not copied" in line and str(b) in line for line in logs)


def test_merge_refuses_to_write_into_a_source(tmp_path: Path) -> None:
    import pytest

    a, b = build_sources(tmp_path)
    with pytest.raises(ValueError, match="also a source"):
        merge_dirs([a, b], a)
