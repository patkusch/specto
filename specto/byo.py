"""Bring your own model: the extract stage without an API key on the machine.

Some teams cannot put a key on the laptop but can paste a request into a chat
window, or run a model behind their own gateway. This module writes every
request the extract stage would send as a JSON file, waits for a person or a
program to answer each one with a JSON file, and then loads the answers back
through the normal merge, renumbering and export.

The loop, in order:

1. `dump_requests` writes `out_dir/requests/chunk_NN.json`, one per chunk of
   frames, plus a README and a manifest.
2. The reader answers `chunk_01.json` by saving the model's JSON as
   `chunk_01.response.json`, then calls `regenerate_chunk_request` for chunk
   2 so it carries the screens found in chunk 1, and so on.
3. When every chunk is answered, `dump_consolidate_request` writes the merge
   request(s), which the reader answers the same way.
4. `load_responses` validates everything, saves the chunk readings the way
   `extract` does, builds the Analysis, renumbers it and writes analysis.json.

`status` says where the loop stands. Nothing here calls a model.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, ValidationError

from specto import extract as _extract
from specto.diff import crop_path_for
from specto.extract import (
    AnalysisResponse,
    ChunkReading,
    RequirementsResponse,
    StructureResponse,
    _consolidation_blocks,
    _image_block,
    build_chunk_content,
    format_transcript,
    renumber,
    save_readings,
    split_chunks,
)
from specto.model import Analysis, Recording, Usage
from specto.prompts import SYSTEM_PROMPT_CONSOLIDATE, SYSTEM_PROMPT_READ

REQUESTS_DIR = "requests"
MANIFEST_NAME = "manifest.json"
README_NAME = "README.md"
CONSOLIDATE_NAME = "consolidate"
CONSOLIDATE_REQUIREMENTS_NAME = "consolidate_requirements"
CONSOLIDATE_STRUCTURE_NAME = "consolidate_structure"
USAGE_MODEL = "bring-your-own"

# These three lines are copied from extract.consolidate so the requests here
# match what the API path sends word for word. If extract changes them, change
# them here too.
ASK_WHOLE = "Produce the complete consolidated analysis."
ASK_REQUIREMENTS = "This recording is long, so the work is split. Produce only the final requirements and their acceptance criteria."
ASK_STRUCTURE = "This recording is long, so the work is split. Produce the title, summary, actors, screens, fields, actions, journey and questions. Requirements were done separately."

HOW_TO_ANSWER = (
    "Give the model `system` as its system prompt. Send `content` as the user message, "
    "block by block in order; for a block of type image, attach the file named in `path` "
    "(the path is relative to the folder that contains requests/). Ask for a JSON object "
    "that matches `schema` exactly, nothing else. Save the JSON the model returns as "
    "`answer_file`, next to this request."
)


class ByoError(RuntimeError):
    """A request or an answer file is missing or unusable."""


# --------------------------------------------------------------------- helpers


def requests_dir(out_dir: Path | str) -> Path:
    return Path(out_dir) / REQUESTS_DIR


def chunk_request_name(n: int) -> str:
    return f"chunk_{n:02d}.json"


def response_name(request_name: str) -> str:
    """chunk_01.json -> chunk_01.response.json"""
    return request_name[: -len(".json")] + ".response.json"


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _read_manifest(out_dir: Path) -> dict:
    path = requests_dir(out_dir) / MANIFEST_NAME
    if not path.exists():
        raise ByoError(f"No {path}. Run dump_requests first (specto requests {out_dir}).")
    return json.loads(path.read_text(encoding="utf-8"))


def _recording_from_manifest(manifest: dict) -> Recording:
    return Recording.model_validate(manifest["recording"])


def _ocr_from_manifest(manifest: dict) -> Optional[dict[int, str]]:
    ocr = manifest.get("ocr_text")
    if not ocr:
        return None
    return {int(k): str(v) for k, v in ocr.items()}


def _image_paths_for(recording: Recording, chunk, out_dir: Path) -> dict[str, str]:
    """base64 data -> relative path, for every image build_chunk_content may add
    for this chunk: each frame and, where one is saved, its close-up crop."""
    keyframes = {k.index: k for k in recording.keyframes}
    lookup: dict[str, str] = {}
    for moment in chunk:
        keyframe = keyframes[moment.keyframe_index]
        candidates = [keyframe.path]
        if keyframe.change_from_previous is not None:
            crop = crop_path_for(keyframe.path)
            if (out_dir / crop).exists():
                candidates.append(crop)
        for rel in candidates:
            data = base64.standard_b64encode((out_dir / rel).read_bytes()).decode("ascii")
            lookup[data] = rel
    return lookup


def content_as_paths(content: list[dict], lookup: dict[str, str]) -> list[dict]:
    """The same block list with each base64 image replaced by {"type": "image", "path": ...}."""
    out: list[dict] = []
    for block in content:
        if block.get("type") == "image":
            data = block["source"]["data"]
            if data not in lookup:
                raise ByoError("An image block in the request does not match any frame or close-up on disk.")
            out.append({"type": "image", "path": lookup[data]})
        else:
            out.append(dict(block))
    return out


def content_with_images(out_dir: Path | str, content: list[dict]) -> list[dict]:
    """The reverse of `content_as_paths`: the block list a ModelCaller accepts,
    with each {"type": "image", "path": ...} read from disk as base64.
    A gateway program can call this and hand the result to any ModelCaller."""
    out_dir = Path(out_dir)
    out: list[dict] = []
    for block in content:
        if block.get("type") == "image" and "path" in block:
            out.append(_image_block(out_dir, block["path"]))
        else:
            out.append(dict(block))
    return out


def _json_object_text(raw: str) -> str:
    """The text from the first '{' to the last '}', so ```json fences and a
    line of chat before the answer do not matter."""
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found (no '{' ... '}')")
    return raw[start : end + 1]


def read_response(path: Path | str, model: type[BaseModel]) -> BaseModel:
    """Parse one answer file as `model`. The error names the file and says what
    is wrong, so the reader can fix the one file and try again."""
    path = Path(path)
    if not path.exists():
        raise ByoError(f"Answer file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(_json_object_text(raw))
    except ValueError as error:
        raise ByoError(f"{path} is not valid JSON: {error}") from error
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise ByoError(f"{path} does not match {model.__name__}:\n{error}") from error


def _answered_chunks(out_dir: Path, chunk_count: int) -> dict[int, ChunkReading]:
    """Every chunk answer file that exists and parses, keyed by chunk number."""
    folder = requests_dir(out_dir)
    readings: dict[int, ChunkReading] = {}
    for n in range(1, chunk_count + 1):
        path = folder / response_name(chunk_request_name(n))
        if path.exists():
            readings[n] = read_response(path, ChunkReading)  # type: ignore[assignment]
    return readings


def _chunk_request(
    recording: Recording,
    chunks: list,
    n: int,
    out_dir: Path,
    earlier: list[ChunkReading],
    ocr_text: Optional[dict[int, str]],
    note: str,
) -> dict:
    chunk = chunks[n - 1]
    content = build_chunk_content(recording, chunk, out_dir, n, len(chunks), earlier, ocr_text=ocr_text)
    lookup = _image_paths_for(recording, chunk, out_dir)
    return {
        "chunk": n,
        "of": len(chunks),
        "frames": [m.keyframe_index for m in chunk],
        "answer_file": response_name(chunk_request_name(n)),
        "how_to_answer": HOW_TO_ANSWER,
        "note": note,
        "system": SYSTEM_PROMPT_READ,
        "output_model": "ChunkReading",
        "schema": ChunkReading.model_json_schema(),
        "content": content_as_paths(content, lookup),
    }


def _is_long(recording: Recording) -> bool:
    return len(format_transcript(recording)) > _extract.LONG_TRANSCRIPT_CHARS


def _consolidate_files(recording: Recording) -> list[tuple[str, str, type[BaseModel]]]:
    """(request name without .json, ask text, response model) for the merge step,
    one file normally and two when the transcript is over LONG_TRANSCRIPT_CHARS."""
    if _is_long(recording):
        return [
            (CONSOLIDATE_REQUIREMENTS_NAME, ASK_REQUIREMENTS, RequirementsResponse),
            (CONSOLIDATE_STRUCTURE_NAME, ASK_STRUCTURE, StructureResponse),
        ]
    return [(CONSOLIDATE_NAME, ASK_WHOLE, AnalysisResponse)]


# ------------------------------------------------------------------- README


def _readme(chunk_count: int, split: bool) -> str:
    merge_files = (
        f"`{CONSOLIDATE_REQUIREMENTS_NAME}.json` and `{CONSOLIDATE_STRUCTURE_NAME}.json` "
        "(this recording is long, so the merge is split in two)"
        if split
        else f"`{CONSOLIDATE_NAME}.json`"
    )
    last = chunk_request_name(chunk_count)
    return f"""# Answering these requests by hand or through your own model

specto normally sends these requests to the Claude API itself. This folder is
the same work, written out as files, for teams that cannot put an API key on
this machine. You (or a program you run) answer each file; specto then builds
the workbook from the answers exactly as it would have from the API.

## What is in here

- `chunk_01.json` to `{last}`: one request per group of frames. Each one holds
  the system prompt, the message to send (text blocks and image blocks in
  order), and the JSON schema the answer must match.
- `{MANIFEST_NAME}`: what specto needs to rebuild a request. Leave it alone.
- Image blocks name a file such as `frames/frame_0007.jpg`. That path is
  relative to the folder above this one, so the images are in `../frames/`.

## The loop

1. Open `chunk_01.json`. Give the model the `system` text as its system prompt.
   Send the `content` blocks as the user message, in order, attaching each
   image where its block sits. Ask for a JSON object that matches `schema`.
2. Save the model's answer as `chunk_01.response.json` in this folder. The
   answer may sit inside a ```json fence or after a line of chat; specto reads
   from the first `{{` to the last `}}`.
3. Each chunk request lists the screens found in earlier chunks, so the model
   reuses their ids instead of inventing new ones. That list cannot be filled
   in before the earlier chunks are answered, so the files written first
   carry an empty list. Before answering chunk 2, run
   `specto requests <out_dir> --regenerate 2` to rebuild `chunk_02.json` from
   the answers so far. Then answer it, regenerate chunk 3, and so on.
   Skipping this step still works; the merge step tidies up duplicate
   screens, but it does a better job when the ids were consistent.
4. When every `chunk_NN.response.json` exists, run `specto requests <out_dir>`
   again. It writes the merge request, {merge_files}. Answer it the same way,
   as `<name>.response.json`.
5. Run `specto load <out_dir>`. specto checks every answer, merges them,
   renumbers the ids, writes `analysis.json` and the workbook.

`specto status <out_dir>` (or `specto requests <out_dir>` with nothing left to
write) says which files are answered and what to do next.

## If an answer is rejected

The message names the file and the field that is wrong. Fix that one file and
run the load again. Nothing else needs redoing.
"""


# --------------------------------------------------------------------- dumping


def dump_requests(
    recording: Recording,
    out_dir: Path | str,
    frames_per_call: int = 8,
    ocr_text: Optional[dict[int, str]] = None,
    log: Callable[[str], None] = print,
) -> Path:
    """Write one request file per chunk, a README and a manifest into
    out_dir/requests/. Returns the requests folder.

    Chunks whose answer file already exists are left alone. Every chunk
    request is built with an empty known-screens list, because earlier
    answers do not exist yet; `regenerate_chunk_request` fills it in later.
    """
    out_dir = Path(out_dir)
    folder = requests_dir(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    chunks = split_chunks(recording, frames_per_call)
    if not chunks:
        raise ByoError("The recording has no moments, so there is nothing to ask the model.")

    manifest = {
        "frames_per_call": frames_per_call,
        "chunk_count": len(chunks),
        "long_transcript": _is_long(recording),
        "ocr_text": {str(k): v for k, v in (ocr_text or {}).items()} or None,
        "recording": recording.model_dump(),
    }
    _write_json(folder / MANIFEST_NAME, manifest)

    note = (
        "The 'Screens identified so far' list is empty because no earlier chunk has been "
        "answered yet. After answering the chunks before this one, regenerate this request "
        "so the list is filled in (see README.md)."
    )
    written = 0
    for n in range(1, len(chunks) + 1):
        path = folder / chunk_request_name(n)
        if (folder / response_name(path.name)).exists():
            log(f"chunk {n}/{len(chunks)}: already answered, request left as is")
            continue
        _write_json(path, _chunk_request(recording, chunks, n, out_dir, [], ocr_text, note))
        written += 1
    (folder / README_NAME).write_text(_readme(len(chunks), _is_long(recording)), encoding="utf-8")
    log(f"wrote {written} chunk request(s) to {folder}; answer them as chunk_NN.response.json")
    return folder


def regenerate_chunk_request(out_dir: Path | str, n: int, log: Callable[[str], None] = print) -> Path:
    """Rebuild chunk n's request with the known-screens list taken from the
    answers already saved for chunks before n. Returns the request path."""
    out_dir = Path(out_dir)
    manifest = _read_manifest(out_dir)
    recording = _recording_from_manifest(manifest)
    chunks = split_chunks(recording, int(manifest["frames_per_call"]))
    if not 1 <= n <= len(chunks):
        raise ByoError(f"There is no chunk {n}; the recording has {len(chunks)} chunk(s).")
    answered = _answered_chunks(out_dir, n - 1)
    earlier = [answered[k] for k in sorted(answered)]
    missing = [k for k in range(1, n) if k not in answered]
    if missing:
        note = (
            f"The 'Screens identified so far' list comes from the answers to chunk(s) "
            f"{', '.join(str(k) for k in sorted(answered)) or 'none'}; chunk(s) "
            f"{', '.join(str(k) for k in missing)} are not answered yet."
        )
    elif earlier:
        note = f"The 'Screens identified so far' list comes from the answers to chunks 1 to {n - 1}."
    else:
        note = "This is the first chunk, so there are no earlier screens to list."
    path = requests_dir(out_dir) / chunk_request_name(n)
    _write_json(path, _chunk_request(recording, chunks, n, out_dir, earlier, _ocr_from_manifest(manifest), note))
    log(f"rebuilt {path} with screens from {len(earlier)} earlier answer(s)")
    return path


def dump_consolidate_request(
    recording: Recording, out_dir: Path | str, log: Callable[[str], None] = print
) -> list[Path]:
    """Write the merge request(s). Needs every chunk answered. Returns the paths.

    One file, `consolidate.json`, asking for an AnalysisResponse; or, when the
    transcript is over LONG_TRANSCRIPT_CHARS, `consolidate_requirements.json`
    and `consolidate_structure.json`, mirroring extract.consolidate.
    """
    out_dir = Path(out_dir)
    manifest = _read_manifest(out_dir)
    chunk_count = int(manifest["chunk_count"])
    answered = _answered_chunks(out_dir, chunk_count)
    missing = [k for k in range(1, chunk_count + 1) if k not in answered]
    if missing:
        raise ByoError(
            "Every chunk must be answered before the merge request can be written; missing: "
            + ", ".join(response_name(chunk_request_name(k)) for k in missing)
        )
    readings = [answered[k] for k in range(1, chunk_count + 1)]
    folder = requests_dir(out_dir)
    paths: list[Path] = []
    for name, ask, model in _consolidate_files(recording):
        path = folder / f"{name}.json"
        _write_json(path, {
            "answer_file": response_name(path.name),
            "how_to_answer": HOW_TO_ANSWER,
            "system": SYSTEM_PROMPT_CONSOLIDATE,
            "output_model": model.__name__,
            "schema": model.model_json_schema(),
            "content": _consolidation_blocks(recording, readings, ask),
        })
        paths.append(path)
        log(f"wrote {path}; answer it as {response_name(path.name)}")
    return paths


# --------------------------------------------------------------------- loading


def load_responses(
    recording: Recording, out_dir: Path | str, log: Callable[[str], None] = print
) -> Analysis:
    """Read every answer file, build the Analysis the way extract.consolidate
    does, renumber it, write analysis.json and return it."""
    out_dir = Path(out_dir)
    manifest = _read_manifest(out_dir)
    frames_per_call = int(manifest["frames_per_call"])
    chunks = split_chunks(recording, frames_per_call)
    folder = requests_dir(out_dir)

    readings: list[ChunkReading] = []
    for n in range(1, len(chunks) + 1):
        path = folder / response_name(chunk_request_name(n))
        readings.append(read_response(path, ChunkReading))  # type: ignore[arg-type]
    save_readings(out_dir / "chunk_readings.json", chunks, readings)
    log(f"loaded {len(readings)} chunk answer(s); saved to {out_dir / 'chunk_readings.json'}")

    files = _consolidate_files(recording)
    parsed = {name: read_response(folder / response_name(f"{name}.json"), model) for name, _ask, model in files}
    # The next four lines mirror extract.consolidate, which builds the
    # Analysis inline; they are copied rather than imported.
    if len(files) == 1:
        response = AnalysisResponse.model_validate(parsed[CONSOLIDATE_NAME].model_dump())
    else:
        parsed_one = parsed[CONSOLIDATE_REQUIREMENTS_NAME]
        parsed_two = parsed[CONSOLIDATE_STRUCTURE_NAME]
        response = AnalysisResponse(**parsed_two.model_dump(), **parsed_one.model_dump())
    analysis = Analysis(**response.model_dump())
    analysis = renumber(analysis, log=log)
    analysis.usage = Usage(model=USAGE_MODEL, calls=len(readings) + len(files))
    path = out_dir / "analysis.json"
    path.write_text(analysis.model_dump_json(indent=2))
    log(f"wrote {path}")
    return analysis


# ---------------------------------------------------------------------- status


def status(out_dir: Path | str) -> str:
    """Which requests exist, which are answered, and what to do next."""
    out_dir = Path(out_dir)
    folder = requests_dir(out_dir)
    manifest_path = folder / MANIFEST_NAME
    if not manifest_path.exists():
        return f"No requests written yet in {folder}.\nNext: run `specto requests {out_dir}`."
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    chunk_count = int(manifest["chunk_count"])
    long_transcript = bool(manifest.get("long_transcript"))
    merge_names = (
        [CONSOLIDATE_REQUIREMENTS_NAME, CONSOLIDATE_STRUCTURE_NAME] if long_transcript else [CONSOLIDATE_NAME]
    )

    lines = [f"Requests in {folder}:"]
    unanswered: list[int] = []
    for n in range(1, chunk_count + 1):
        request = folder / chunk_request_name(n)
        answer = folder / response_name(request.name)
        if answer.exists():
            state = "answered"
        elif request.exists():
            state = "waiting for an answer"
            unanswered.append(n)
        else:
            state = "request not written"
            unanswered.append(n)
        lines.append(f"  {request.name}: {state}")

    merge_written = all((folder / f"{name}.json").exists() for name in merge_names)
    merge_answered = all((folder / response_name(f"{name}.json")).exists() for name in merge_names)
    for name in merge_names:
        request = folder / f"{name}.json"
        answer = folder / response_name(request.name)
        if answer.exists():
            lines.append(f"  {request.name}: answered")
        elif request.exists():
            lines.append(f"  {request.name}: waiting for an answer")
        else:
            lines.append(f"  {request.name}: not written yet")
    analysis_exists = (out_dir / "analysis.json").exists()
    lines.append(f"  analysis.json: {'written' if analysis_exists else 'not written yet'}")

    if unanswered:
        first = unanswered[0]
        if first > 1:
            lines.append(
                f"Next: run `specto requests {out_dir} --regenerate {first}` so chunk {first} lists the screens "
                f"found so far, then answer {chunk_request_name(first)} as "
                f"{response_name(chunk_request_name(first))}."
            )
        else:
            lines.append(f"Next: answer {chunk_request_name(first)} as {response_name(chunk_request_name(first))}.")
    elif not merge_written:
        lines.append(f"Next: every chunk is answered; run `specto requests {out_dir}` to write the merge request.")
    elif not merge_answered:
        names = ", ".join(response_name(f"{name}.json") for name in merge_names)
        lines.append(f"Next: answer the merge request as {names}.")
    elif not analysis_exists:
        lines.append(f"Next: run `specto load {out_dir}` to build analysis.json and the workbook.")
    else:
        lines.append("Done: analysis.json is written. Run `specto export` again if the workbook needs rebuilding.")
    return "\n".join(lines)
