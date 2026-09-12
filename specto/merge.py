"""Merge two or more finished runs into one output folder, without the model.

An expert often walks a process over two or three sessions, or two experts
cover different parts. Each run lands in its own folder with its own
analysis.json and recording.json. `merge_dirs` plays the recordings back to
back (frames renumbered, times shifted), joins the analyses, folds together
the screens, fields, requirements and questions that appear in more than one
session, and writes a fresh analysis.json and recording.json. The caller then
exports as usual.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Callable, Optional

from specto.diff import crop_path_for
from specto.extract import renumber
from specto.model import (
    AcceptanceCriterion,
    Action,
    Analysis,
    DataField,
    JourneyStep,
    Keyframe,
    Moment,
    Question,
    Recording,
    Requirement,
    Screen,
    TranscriptSegment,
    Usage,
)

Log = Callable[[str], None]

COUNTED = (
    ("screens", "screens"),
    ("fields", "fields"),
    ("actions", "actions"),
    ("journey", "journey steps"),
    ("requirements", "requirements"),
    ("acceptance_criteria", "acceptance criteria"),
    ("questions", "questions"),
)


# ------------------------------------------------------------------ loading


def load_run(source: Path | str) -> tuple[Analysis, Recording]:
    """Read analysis.json and recording.json from one run folder."""
    source = Path(source)
    analysis_path = source / "analysis.json"
    recording_path = source / "recording.json"
    for path in (analysis_path, recording_path):
        if not path.exists():
            raise FileNotFoundError(f"{path} not found; is {source} a finished specto run?")
    return (
        Analysis.model_validate_json(analysis_path.read_text()),
        Recording.model_validate_json(recording_path.read_text()),
    )


def _load_ocr(source: Path) -> Optional[dict[int, str]]:
    path = source / "ocr.json"
    if not path.exists():
        return None
    try:
        return {int(k): str(v) for k, v in json.loads(path.read_text()).items()}
    except (json.JSONDecodeError, ValueError, AttributeError):
        return None


# ------------------------------------------------------------------ recording


def _shift_segment(segment: TranscriptSegment, offset: float) -> TranscriptSegment:
    moved = segment.model_copy(deep=True)
    moved.start += offset
    moved.end += offset
    for word in moved.words:
        word.start += offset
        word.end += offset
    return moved


def _copy_frame(source: Path, out_dir: Path, keyframe: Keyframe, new_index: int, log: Log) -> str:
    """Copy one still (and its close-up, when there is one) under the new number.
    Returns the new relative path."""
    suffix = Path(keyframe.path).suffix or ".jpg"
    new_rel = f"frames/frame_{new_index:04d}{suffix}"
    old_file = source / keyframe.path
    if old_file.exists():
        shutil.copyfile(old_file, out_dir / new_rel)
    else:
        log(f"frame {keyframe.index} of {source} is missing on disk ({old_file}); the link will be broken")
    old_crop = source / crop_path_for(keyframe.path)
    if old_crop.exists():
        shutil.copyfile(old_crop, out_dir / crop_path_for(new_rel))
    return new_rel


def _merge_recordings(
    sources: list[Path], recordings: list[Recording], out_dir: Path, log: Log
) -> tuple[Recording, list[dict[int, int]], list[float]]:
    """Concatenate the recordings as if played back to back.

    Returns the merged recording, one old->new keyframe index map per source,
    and one time offset per source.
    """
    (out_dir / "frames").mkdir(parents=True, exist_ok=True)
    keyframes: list[Keyframe] = []
    segments: list[TranscriptSegment] = []
    moments: list[Moment] = []
    index_maps: list[dict[int, int]] = []
    time_offsets: list[float] = []
    ocr_texts: list[Optional[dict[int, str]]] = []
    time_offset = 0.0

    for source, recording in zip(sources, recordings):
        index_map: dict[int, int] = {}
        for keyframe in recording.keyframes:
            new_index = len(keyframes)
            index_map[keyframe.index] = new_index
            new_rel = _copy_frame(source, out_dir, keyframe, new_index, log)
            moved = keyframe.model_copy(deep=True)
            moved.index = new_index
            moved.timestamp += time_offset
            moved.path = new_rel
            keyframes.append(moved)
        segments.extend(_shift_segment(s, time_offset) for s in recording.segments)
        for moment in recording.moments:
            moved_moment = Moment(
                keyframe_index=_map_index(index_map, moment.keyframe_index, len(keyframes) - len(recording.keyframes), log, f"moment at {moment.start:.0f}s of {source}"),
                start=moment.start + time_offset,
                end=moment.end + time_offset,
                segments=[_shift_segment(s, time_offset) for s in moment.segments],
            )
            moments.append(moved_moment)
        index_maps.append(index_map)
        time_offsets.append(time_offset)
        ocr_texts.append(_load_ocr(source))
        time_offset += recording.duration

    transcript_sources = {r.transcript_source for r in recordings}
    merged = Recording(
        source="merged: " + " + ".join(r.source for r in recordings),
        duration=time_offset,
        keyframes=keyframes,
        segments=segments,
        moments=moments,
        transcript_source="file" if transcript_sources == {"file"} else recordings[0].transcript_source,
    )

    if all(text is not None for text in ocr_texts):
        merged_ocr: dict[str, str] = {}
        for index_map, text in zip(index_maps, ocr_texts):
            for old_index, value in (text or {}).items():
                if old_index in index_map:
                    merged_ocr[str(index_map[old_index])] = value
        (out_dir / "ocr.json").write_text(json.dumps(merged_ocr, indent=2, ensure_ascii=False))
    else:
        missing = [str(s) for s, text in zip(sources, ocr_texts) if text is None]
        log(f"ocr.json not copied: {', '.join(missing)} has none")
    return merged, index_maps, time_offsets


def _map_index(index_map: dict[int, int], old: int, frame_offset: int, log: Log, what: str) -> int:
    new = index_map.get(old)
    if new is None:
        log(f"{what} points at frame {old}, which the recording does not list; shifted by {frame_offset}")
        return old + frame_offset
    return new


# ------------------------------------------------------------------ analysis


def _prefix_for(position: int) -> str:
    """0 -> A, 1 -> B, ... 25 -> Z, 26 -> AA."""
    letters = ""
    n = position
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            return letters


def _rebase(
    analysis: Analysis, prefix: str, index_map: dict[int, int], time_offset: float, log: Log, source: Path
) -> Analysis:
    """A deep copy with every id prefixed, every frame index mapped and every time shifted."""
    a = analysis.model_copy(deep=True)
    frame_offset = min(index_map.values()) if index_map else 0

    def pid(old: Optional[str]) -> Optional[str]:
        return f"{prefix}-{old}" if old else old

    def frame(old: int, what: str) -> int:
        return _map_index(index_map, old, frame_offset, log, f"{what} in {source}")

    for screen in a.screens:
        screen.id = pid(screen.id)
        screen.keyframe_indexes = [frame(i, f"screen {screen.name}") for i in screen.keyframe_indexes]
        screen.first_seen += time_offset
        screen.field_ids = [pid(f) for f in screen.field_ids]
        screen.action_ids = [pid(x) for x in screen.action_ids]
    for field in a.fields:
        field.id = pid(field.id)
        field.screen_id = pid(field.screen_id)
        field.keyframe_index = frame(field.keyframe_index, f"field {field.label}")
        field.timestamp += time_offset
    for action in a.actions:
        action.id = pid(action.id)
        action.screen_id = pid(action.screen_id)
        action.leads_to_screen_id = pid(action.leads_to_screen_id)
        action.keyframe_index = frame(action.keyframe_index, f"action {action.description}")
        action.timestamp += time_offset
    for step in a.journey:
        step.screen_id = pid(step.screen_id)
        step.keyframe_index = frame(step.keyframe_index, f"journey step {step.order}")
        step.timestamp += time_offset
    for requirement in a.requirements:
        requirement.id = pid(requirement.id)
        requirement.screen_id = pid(requirement.screen_id)
        requirement.keyframe_index = frame(requirement.keyframe_index, f"requirement {requirement.id}")
        requirement.timestamp += time_offset
    for criterion in a.acceptance_criteria:
        criterion.id = pid(criterion.id)
        criterion.requirement_id = pid(criterion.requirement_id)
        criterion.keyframe_index = frame(criterion.keyframe_index, f"criterion {criterion.id}")
        criterion.timestamp += time_offset
    for question in a.questions:
        question.id = pid(question.id)
        question.screen_id = pid(question.screen_id)
        question.keyframe_index = frame(question.keyframe_index, f"question {question.id}")
        question.timestamp += time_offset
    return a


def _norm(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _union(first: list, second: list) -> list:
    return first + [x for x in second if x not in first]


def _merge_screens(screens: list[Screen]) -> tuple[list[Screen], dict[str, str]]:
    """Fold screens with the same name into the earliest one. Returns the kept
    screens and a map from every dropped id to the id it now means."""
    kept: dict[str, Screen] = {}
    remap: dict[str, str] = {}
    for screen in screens:
        key = _norm(screen.name)
        earlier = kept.get(key)
        if earlier is None:
            kept[key] = screen
            continue
        earlier.keyframe_indexes = sorted(set(earlier.keyframe_indexes) | set(screen.keyframe_indexes))
        earlier.first_seen = min(earlier.first_seen, screen.first_seen)
        earlier.field_ids = _union(earlier.field_ids, screen.field_ids)
        earlier.action_ids = _union(earlier.action_ids, screen.action_ids)
        remap[screen.id] = earlier.id
    return list(kept.values()), remap


def _dedupe(items: list, key: Callable) -> tuple[list, dict[str, str]]:
    """Keep the first item for each key; map every dropped id to the kept id."""
    kept: dict = {}
    remap: dict[str, str] = {}
    for item in items:
        k = key(item)
        if k in kept:
            remap[item.id] = kept[k].id
        else:
            kept[k] = item
    return list(kept.values()), remap


def _sum_usage(analyses: list[Analysis]) -> Optional[Usage]:
    usages = [a.usage for a in analyses if a.usage is not None]
    if not usages:
        return None
    models = []
    for u in usages:
        if u.model not in models:
            models.append(u.model)
    total = Usage(model=" + ".join(models))
    for u in usages:
        total.calls += u.calls
        total.input_tokens += u.input_tokens
        total.output_tokens += u.output_tokens
        total.cache_read_input_tokens += u.cache_read_input_tokens
        total.cache_creation_input_tokens += u.cache_creation_input_tokens
    return total


def _merge_analyses(
    sources: list[Path],
    analyses: list[Analysis],
    index_maps: list[dict[int, int]],
    time_offsets: list[float],
    log: Log,
) -> Analysis:
    rebased = [
        _rebase(a, _prefix_for(i), index_maps[i], time_offsets[i], log, sources[i])
        for i, a in enumerate(analyses)
    ]

    screens: list[Screen] = [s for a in rebased for s in a.screens]
    fields: list[DataField] = [f for a in rebased for f in a.fields]
    actions: list[Action] = [x for a in rebased for x in a.actions]
    journey: list[JourneyStep] = [j for a in rebased for j in a.journey]
    requirements: list[Requirement] = [r for a in rebased for r in a.requirements]
    criteria: list[AcceptanceCriterion] = [c for a in rebased for c in a.acceptance_criteria]
    questions: list[Question] = [q for a in rebased for q in a.questions]

    screens, screen_map = _merge_screens(screens)

    def screen_ref(old: Optional[str]) -> Optional[str]:
        return screen_map.get(old, old) if old else old

    for field in fields:
        field.screen_id = screen_ref(field.screen_id)
    for action in actions:
        action.screen_id = screen_ref(action.screen_id)
        action.leads_to_screen_id = screen_ref(action.leads_to_screen_id)
    for step in journey:
        step.screen_id = screen_ref(step.screen_id)
    for requirement in requirements:
        requirement.screen_id = screen_ref(requirement.screen_id)
    for question in questions:
        question.screen_id = screen_ref(question.screen_id)

    fields, field_map = _dedupe(fields, lambda f: (f.screen_id, _norm(f.label)))
    for screen in screens:
        screen.field_ids = _union([], [field_map.get(f, f) for f in screen.field_ids])

    requirements, requirement_map = _dedupe(requirements, lambda r: _norm(r.statement))
    for criterion in criteria:
        criterion.requirement_id = requirement_map.get(criterion.requirement_id, criterion.requirement_id)

    questions, _ = _dedupe(questions, lambda q: _norm(q.question))

    actors: list[str] = []
    seen_actors: set[str] = set()
    for a in rebased:
        for actor in a.actors:
            if _norm(actor) not in seen_actors:
                seen_actors.add(_norm(actor))
                actors.append(actor)

    merged = Analysis(
        title=f"{analyses[0].title} (merged from {len(analyses)} sessions)",
        summary="\n\n".join(a.summary.strip() for a in analyses if a.summary.strip()),
        actors=actors,
        screens=screens,
        fields=fields,
        actions=actions,
        journey=journey,
        requirements=requirements,
        acceptance_criteria=criteria,
        questions=questions,
        usage=_sum_usage(analyses),
    )
    return renumber(merged, log=log)


# ------------------------------------------------------------------ entry points


def merge_dirs(sources: list[Path | str], out_dir: Path | str, log: Log = print) -> tuple[Analysis, Recording]:
    """Merge finished runs into out_dir and write analysis.json and recording.json there.

    The recordings are joined as if played back to back; the analyses are
    joined and their duplicates (same screen name, same field label on the
    same screen, same requirement statement, same question) folded together.
    Nothing is exported here; call export_all on the result.
    """
    if not sources:
        raise ValueError("merge needs at least one run folder")
    source_paths = [Path(s) for s in sources]
    out_dir = Path(out_dir)
    for src in source_paths:
        if src.resolve() == out_dir.resolve():
            raise ValueError(f"the output folder {out_dir} is also a source; pick a new folder")
    runs = [load_run(src) for src in source_paths]
    analyses = [a for a, _ in runs]
    recordings = [r for _, r in runs]
    out_dir.mkdir(parents=True, exist_ok=True)

    recording, index_maps, time_offsets = _merge_recordings(source_paths, recordings, out_dir, log)
    analysis = _merge_analyses(source_paths, analyses, index_maps, time_offsets, log)

    (out_dir / "recording.json").write_text(recording.model_dump_json(indent=2))
    (out_dir / "analysis.json").write_text(analysis.model_dump_json(indent=2))
    log(
        f"merged {len(source_paths)} runs into {out_dir}: {len(recording.keyframes)} frames, "
        f"{len(analysis.screens)} screens, {len(analysis.requirements)} requirements, "
        f"{len(analysis.questions)} questions"
    )
    return analysis, recording


def merge_report(sources: list[Path | str], analysis: Analysis) -> str:
    """A few plain lines: what each run brought in and what is left after duplicates were folded."""
    source_paths = [Path(s) for s in sources]
    lines = [f"Merged {len(source_paths)} runs."]
    totals = {attr: 0 for attr, _ in COUNTED}
    for src in source_paths:
        a, _ = load_run(src)
        parts = []
        for attr, label in COUNTED:
            n = len(getattr(a, attr))
            totals[attr] += n
            parts.append(f"{n} {label}")
        lines.append(f"  {src}: " + ", ".join(parts))
    lines.append("After merging duplicates:")
    for attr, label in COUNTED:
        kept = len(getattr(analysis, attr))
        folded = totals[attr] - kept
        note = f" ({folded} folded into an earlier one)" if folded > 0 else ""
        lines.append(f"  {label}: {totals[attr]} came in, {kept} kept{note}")
    return "\n".join(lines)
