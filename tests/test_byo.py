"""Offline tests for the bring-your-own-model path. No API key, no network."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from specto import byo
from specto import extract as extract_module
from specto.byo import (
    ByoError,
    content_with_images,
    dump_consolidate_request,
    dump_requests,
    load_responses,
    regenerate_chunk_request,
    status,
)
from specto.extract import (
    AnalysisResponse,
    ChunkReading,
    RequirementsResponse,
    StructureResponse,
    build_chunk_content,
    chunk_key,
    load_prior_readings,
    split_chunks,
)
from specto.fake import FakeCaller
from specto.model import Analysis, Keyframe, Moment, Recording, TranscriptSegment

FRAME_COUNT = 10


def make_recording(out_dir: Path) -> Recording:
    """Ten tiny JPEG frames, one every 10 seconds, with a few spoken lines."""
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True)
    keyframes = []
    for i in range(FRAME_COUNT):
        rel = f"frames/frame_{i:04d}.jpg"
        Image.new("RGB", (32, 24), (i * 20, 100, 200)).save(out_dir / rel, "JPEG")
        keyframes.append(Keyframe(index=i, timestamp=10.0 * i, path=rel, width=32, height=24))
    segments = [
        TranscriptSegment(start=1.0, end=8.0, text="This is the customer screen."),
        TranscriptSegment(start=12.0, end=18.0, text="Here we type the postcode, it is mandatory."),
        TranscriptSegment(start=41.0, end=48.0, text="Then we press Save."),
        TranscriptSegment(start=72.0, end=79.0, text="And it goes to the approvals queue."),
    ]
    moments = []
    for i in range(FRAME_COUNT):
        start, end = 10.0 * i, 10.0 * (i + 1)
        moments.append(
            Moment(
                keyframe_index=i,
                start=start,
                end=end,
                segments=[s for s in segments if start <= s.start < end],
            )
        )
    return Recording(
        source="walkthrough.mp4",
        duration=100.0,
        keyframes=keyframes,
        segments=segments,
        moments=moments,
        transcript_source="file",
    )


def text_of(content_blocks: list[dict]) -> str:
    return "\n".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")


def read_request(out_dir: Path, name: str) -> dict:
    return json.loads((out_dir / "requests" / name).read_text(encoding="utf-8"))


def answer_chunk(out_dir: Path, n: int, fenced: bool = False) -> ChunkReading:
    """Answer chunk n's request the way a person pasting into a chat would:
    rebuild the content with real images, let FakeCaller play the model, save."""
    request = read_request(out_dir, f"chunk_{n:02d}.json")
    content = content_with_images(out_dir, request["content"])
    reading, _usage = FakeCaller()(request["system"], content, ChunkReading)
    body = reading.model_dump_json(indent=2)
    if fenced:
        body = "Here is the JSON you asked for:\n```json\n" + body + "\n```\nLet me know if you need more."
    (out_dir / "requests" / request["answer_file"]).write_text(body, encoding="utf-8")
    return reading


def answer_all_chunks(out_dir: Path, count: int) -> list[ChunkReading]:
    readings = []
    for n in range(1, count + 1):
        if n > 1:
            regenerate_chunk_request(out_dir, n, log=lambda _: None)
        readings.append(answer_chunk(out_dir, n, fenced=(n == 2)))
    return readings


def answer_merge(out_dir: Path, name: str, model) -> None:
    request = read_request(out_dir, name)
    assert request["output_model"] == model.__name__
    parsed, _usage = FakeCaller()(request["system"], request["content"], model)
    (out_dir / "requests" / request["answer_file"]).write_text(parsed.model_dump_json(indent=2), encoding="utf-8")


# ------------------------------------------------------------------- dumping


def test_dump_requests_writes_one_file_per_chunk(tmp_path: Path) -> None:
    recording = make_recording(tmp_path)

    folder = dump_requests(recording, tmp_path, frames_per_call=4, log=lambda _: None)

    assert folder == tmp_path / "requests"
    names = sorted(p.name for p in folder.glob("chunk_*.json"))
    assert names == ["chunk_01.json", "chunk_02.json", "chunk_03.json"]
    assert (folder / "README.md").exists()
    assert "chunk_01.response.json" in (folder / "README.md").read_text()
    assert (folder / "manifest.json").exists()

    expected_frames = [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]
    chunks = split_chunks(recording, 4)
    for n, frames in enumerate(expected_frames, start=1):
        request = read_request(tmp_path, f"chunk_{n:02d}.json")
        assert request["system"] == extract_module.SYSTEM_PROMPT_READ
        assert request["output_model"] == "ChunkReading"
        assert request["schema"] == ChunkReading.model_json_schema()
        assert request["answer_file"] == f"chunk_{n:02d}.response.json"
        assert request["frames"] == frames
        assert "empty" in request["note"]
        images = [b for b in request["content"] if b["type"] == "image"]
        assert [b["path"] for b in images] == [f"frames/frame_{i:04d}.jpg" for i in frames]
        assert all("source" not in b for b in images)
        # Text blocks are exactly what the API path would send.
        real = build_chunk_content(recording, chunks[n - 1], tmp_path, n, 3, [])
        assert [b for b in request["content"] if b["type"] == "text"] == [b for b in real if b["type"] == "text"]
        assert len(request["content"]) == len(real)
        assert "none yet" in text_of(request["content"])
    # The file stays small: no base64 in it.
    assert (folder / "chunk_01.json").stat().st_size < 20_000


def test_content_with_images_rebuilds_the_api_blocks(tmp_path: Path) -> None:
    recording = make_recording(tmp_path)
    dump_requests(recording, tmp_path, frames_per_call=4, log=lambda _: None)
    request = read_request(tmp_path, "chunk_01.json")

    rebuilt = content_with_images(tmp_path, request["content"])

    real = build_chunk_content(recording, split_chunks(recording, 4)[0], tmp_path, 1, 3, [])
    assert rebuilt == real


def test_dump_requests_carries_ocr_text(tmp_path: Path) -> None:
    recording = make_recording(tmp_path)
    dump_requests(recording, tmp_path, frames_per_call=4, ocr_text={1: "Postcode  SW1A 1AA"}, log=lambda _: None)

    assert "Postcode  SW1A 1AA" in text_of(read_request(tmp_path, "chunk_01.json")["content"])
    answer_chunk(tmp_path, 1)
    regenerate_chunk_request(tmp_path, 2, log=lambda _: None)
    # Regeneration keeps the OCR text the dump was given, without being told again.
    dump_again = read_request(tmp_path, "chunk_01.json")
    assert "Postcode  SW1A 1AA" in text_of(dump_again["content"])


def test_dump_requests_leaves_answered_chunks_alone(tmp_path: Path) -> None:
    recording = make_recording(tmp_path)
    dump_requests(recording, tmp_path, frames_per_call=4, log=lambda _: None)
    answer_chunk(tmp_path, 1)
    regenerate_chunk_request(tmp_path, 2, log=lambda _: None)
    before = (tmp_path / "requests" / "chunk_02.json").read_text()

    logged: list[str] = []
    dump_requests(recording, tmp_path, frames_per_call=4, log=logged.append)

    assert any("already answered" in line for line in logged)
    # An unanswered chunk is rewritten with the empty list; that is the documented behaviour.
    assert (tmp_path / "requests" / "chunk_02.json").read_text() != before
    assert (tmp_path / "requests" / "chunk_01.response.json").exists()


# --------------------------------------------------------------- regenerating


def test_regenerate_uses_earlier_answers_for_known_screens(tmp_path: Path) -> None:
    recording = make_recording(tmp_path)
    dump_requests(recording, tmp_path, frames_per_call=4, log=lambda _: None)
    first = answer_chunk(tmp_path, 1)
    assert first.screens[0].id == "S01"

    path = regenerate_chunk_request(tmp_path, 2, log=lambda _: None)

    assert path == tmp_path / "requests" / "chunk_02.json"
    request = read_request(tmp_path, "chunk_02.json")
    text = text_of(request["content"])
    assert "Screens identified so far" in text
    assert "S01 Screen at frame 0" in text
    assert "S04 Screen at frame 3" in text
    assert "none yet" not in text
    assert "chunks 1 to 1" in request["note"]
    # It is exactly what read_chunks would have sent for chunk 2.
    real = build_chunk_content(recording, split_chunks(recording, 4)[1], tmp_path, 2, 3, [first])
    assert content_with_images(tmp_path, request["content"]) == real


def test_regenerate_says_which_earlier_chunks_are_missing(tmp_path: Path) -> None:
    recording = make_recording(tmp_path)
    dump_requests(recording, tmp_path, frames_per_call=4, log=lambda _: None)
    answer_chunk(tmp_path, 1)

    regenerate_chunk_request(tmp_path, 3, log=lambda _: None)

    request = read_request(tmp_path, "chunk_03.json")
    assert "chunk(s) 2 are not answered yet" in request["note"]
    assert "S01 Screen at frame 0" in text_of(request["content"])

    with pytest.raises(ByoError, match="no chunk 4"):
        regenerate_chunk_request(tmp_path, 4, log=lambda _: None)


# ------------------------------------------------------------- consolidating


def test_consolidate_request_needs_every_chunk_answered(tmp_path: Path) -> None:
    recording = make_recording(tmp_path)
    dump_requests(recording, tmp_path, frames_per_call=4, log=lambda _: None)
    answer_chunk(tmp_path, 1)

    with pytest.raises(ByoError, match="chunk_02.response.json"):
        dump_consolidate_request(recording, tmp_path, log=lambda _: None)


def test_full_loop_writes_a_consistent_analysis(tmp_path: Path) -> None:
    recording = make_recording(tmp_path)
    dump_requests(recording, tmp_path, frames_per_call=4, log=lambda _: None)
    readings = answer_all_chunks(tmp_path, 3)

    paths = dump_consolidate_request(recording, tmp_path, log=lambda _: None)
    assert paths == [tmp_path / "requests" / "consolidate.json"]
    request = read_request(tmp_path, "consolidate.json")
    assert request["system"] == extract_module.SYSTEM_PROMPT_CONSOLIDATE
    assert request["output_model"] == "AnalysisResponse"
    assert request["schema"] == AnalysisResponse.model_json_schema()
    real = extract_module._consolidation_blocks(recording, readings, "Produce the complete consolidated analysis.")
    assert request["content"] == real

    answer_merge(tmp_path, "consolidate.json", AnalysisResponse)
    analysis = load_responses(recording, tmp_path, log=lambda _: None)

    assert (tmp_path / "analysis.json").exists()
    reloaded = Analysis.model_validate_json((tmp_path / "analysis.json").read_text())
    assert reloaded == analysis
    assert analysis.usage is not None
    assert analysis.usage.model == "bring-your-own"
    assert analysis.usage.input_tokens == 0
    assert analysis.usage.output_tokens == 0
    assert analysis.usage.calls == 4

    assert [s.id for s in analysis.screens] == ["S01", "S02"]
    assert [f.id for f in analysis.fields] == ["F001", "F002"]
    assert [a.id for a in analysis.actions] == ["A001"]
    assert [r.id for r in analysis.requirements] == ["R001", "R002"]
    assert [c.id for c in analysis.acceptance_criteria] == ["AC001", "AC002", "AC003"]
    assert [q.id for q in analysis.questions] == ["Q001", "Q002"]
    assert [step.order for step in analysis.journey] == [1, 2]

    screen_ids = {s.id for s in analysis.screens}
    requirement_ids = {r.id for r in analysis.requirements}
    for field in analysis.fields:
        assert field.screen_id in screen_ids
    for action in analysis.actions:
        assert action.screen_id in screen_ids
        assert action.leads_to_screen_id in screen_ids
    for step in analysis.journey:
        assert step.screen_id in screen_ids
    for requirement in analysis.requirements:
        assert requirement.screen_id in screen_ids
    for criterion in analysis.acceptance_criteria:
        assert criterion.requirement_id in requirement_ids
    for question in analysis.questions:
        assert question.screen_id in screen_ids
        assert set(question.blocks_requirement_ids) <= requirement_ids
    assert analysis.screens[0].field_ids == ["F001"]
    assert analysis.screens[0].action_ids == ["A001"]

    # The chunk readings are saved so extract(..., reuse_readings=True) finds them.
    prior = load_prior_readings(tmp_path / "chunk_readings.json")
    assert len(prior) == 3
    for chunk, reading in zip(split_chunks(recording, 4), readings):
        assert prior[chunk_key(chunk)] == reading


def test_full_loop_matches_the_api_path(tmp_path: Path) -> None:
    """Answering the files with FakeCaller gives the same analysis as extract with FakeCaller."""
    recording = make_recording(tmp_path)
    dump_requests(recording, tmp_path, frames_per_call=4, log=lambda _: None)
    answer_all_chunks(tmp_path, 3)
    dump_consolidate_request(recording, tmp_path, log=lambda _: None)
    answer_merge(tmp_path, "consolidate.json", AnalysisResponse)
    ours = load_responses(recording, tmp_path, log=lambda _: None)

    api_dir = tmp_path / "api"
    api_recording = make_recording(api_dir)
    theirs = extract_module.extract(api_recording, api_dir, caller=FakeCaller(), frames_per_call=4, log=lambda _: None)

    ours.usage = theirs.usage = None
    assert ours == theirs


def test_long_transcript_splits_the_merge_in_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract_module, "LONG_TRANSCRIPT_CHARS", 10)
    recording = make_recording(tmp_path)
    dump_requests(recording, tmp_path, frames_per_call=4, log=lambda _: None)
    assert "consolidate_requirements.json" in (tmp_path / "requests" / "README.md").read_text()
    answer_all_chunks(tmp_path, 3)

    paths = dump_consolidate_request(recording, tmp_path, log=lambda _: None)

    assert [p.name for p in paths] == ["consolidate_requirements.json", "consolidate_structure.json"]
    one = read_request(tmp_path, "consolidate_requirements.json")
    two = read_request(tmp_path, "consolidate_structure.json")
    assert one["schema"] == RequirementsResponse.model_json_schema()
    assert two["schema"] == StructureResponse.model_json_schema()
    assert "only the final requirements" in one["content"][-1]["text"]
    assert "Requirements were done separately" in two["content"][-1]["text"]

    answer_merge(tmp_path, "consolidate_requirements.json", RequirementsResponse)
    with pytest.raises(ByoError, match="consolidate_structure.response.json"):
        load_responses(recording, tmp_path, log=lambda _: None)
    answer_merge(tmp_path, "consolidate_structure.json", StructureResponse)

    analysis = load_responses(recording, tmp_path, log=lambda _: None)
    assert analysis.title == "Fake walkthrough"
    assert [r.id for r in analysis.requirements] == ["R001", "R002"]
    assert [c.requirement_id for c in analysis.acceptance_criteria] == ["R001", "R001", "R002"]
    assert "consolidate_requirements.json: answered" in status(tmp_path)


# --------------------------------------------------------------------- status


def test_status_before_and_after(tmp_path: Path) -> None:
    recording = make_recording(tmp_path)

    text = status(tmp_path)
    assert "No requests written yet" in text
    assert "specto requests" in text

    dump_requests(recording, tmp_path, frames_per_call=4, log=lambda _: None)
    text = status(tmp_path)
    assert "chunk_01.json: waiting for an answer" in text
    assert "chunk_03.json: waiting for an answer" in text
    assert "consolidate.json: not written yet" in text
    assert "Next: answer chunk_01.json as chunk_01.response.json" in text

    answer_chunk(tmp_path, 1)
    text = status(tmp_path)
    assert "chunk_01.json: answered" in text
    assert "--regenerate 2" in text

    answer_all_chunks(tmp_path, 3)
    text = status(tmp_path)
    assert "every chunk is answered" in text

    dump_consolidate_request(recording, tmp_path, log=lambda _: None)
    text = status(tmp_path)
    assert "consolidate.json: waiting for an answer" in text
    assert "answer the merge request as consolidate.response.json" in text

    answer_merge(tmp_path, "consolidate.json", AnalysisResponse)
    text = status(tmp_path)
    assert "specto load" in text

    load_responses(recording, tmp_path, log=lambda _: None)
    text = status(tmp_path)
    assert "analysis.json: written" in text
    assert text.splitlines()[-1].startswith("Done")


# --------------------------------------------------------------------- errors


def test_bad_response_file_names_the_file(tmp_path: Path) -> None:
    recording = make_recording(tmp_path)
    dump_requests(recording, tmp_path, frames_per_call=4, log=lambda _: None)
    answer_all_chunks(tmp_path, 3)
    bad = tmp_path / "requests" / "chunk_02.response.json"
    bad.write_text('{"screens": [{"id": "S01"}]}', encoding="utf-8")

    with pytest.raises(ByoError) as caught:
        dump_consolidate_request(recording, tmp_path, log=lambda _: None)
    message = str(caught.value)
    assert "chunk_02.response.json" in message
    assert "ChunkReading" in message
    assert "name" in message  # the pydantic message says which field is missing

    bad.write_text("the model said nothing useful", encoding="utf-8")
    with pytest.raises(ByoError, match="chunk_02.response.json is not valid JSON"):
        load_responses(recording, tmp_path, log=lambda _: None)

    bad.unlink()
    with pytest.raises(ByoError, match="Answer file not found: .*chunk_02.response.json"):
        load_responses(recording, tmp_path, log=lambda _: None)


def test_load_without_requests_says_what_to_run(tmp_path: Path) -> None:
    recording = make_recording(tmp_path)
    with pytest.raises(ByoError, match="dump_requests first"):
        load_responses(recording, tmp_path, log=lambda _: None)


def test_dump_with_no_moments_is_an_error(tmp_path: Path) -> None:
    recording = Recording(source="empty.mp4", duration=0.0)
    with pytest.raises(ByoError, match="no moments"):
        dump_requests(recording, tmp_path, log=lambda _: None)
    assert byo.requests_dir(tmp_path).exists()
