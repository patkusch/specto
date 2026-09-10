"""Offline tests for stage 2 (extract). No API key, no network."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from specto import extract as extract_module
from specto.extract import ChunkReading, ClaudeCaller, extract, read_chunks
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
    return "\n".join(b["text"] for b in content_blocks if b["type"] == "text")


def test_read_chunks_builds_one_call_per_chunk(tmp_path: Path) -> None:
    recording = make_recording(tmp_path)
    caller = FakeCaller()

    readings = read_chunks(recording, tmp_path, caller, frames_per_call=4, log=lambda _: None)

    assert len(caller.calls) == 3
    assert len(readings) == 3
    expected = [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]
    for call, frame_indexes in zip(caller.calls, expected):
        assert call["output_model"] is ChunkReading
        text = text_of(call["content_blocks"])
        markers = [int(m) for m in re.findall(r"Frame (\d+) at \d\d:\d\d", text)]
        assert markers == frame_indexes
        images = [b for b in call["content_blocks"] if b["type"] == "image"]
        assert len(images) == len(frame_indexes)
        for image in images:
            assert image["source"]["media_type"] == "image/jpeg"
            raw = base64.b64decode(image["source"]["data"])
            assert raw[:2] == b"\xff\xd8"  # JPEG magic bytes
        # The image for a frame comes right after its "Frame N" marker.
        blocks = call["content_blocks"]
        for i, block in enumerate(blocks):
            if block["type"] == "text" and block["text"].startswith("Frame "):
                assert blocks[i + 1]["type"] == "image"

    first_text = text_of(caller.calls[0]["content_blocks"])
    assert "Frame 0 at 00:00" in first_text
    assert "This is the customer screen." in first_text
    assert "(nothing said)" in first_text
    assert "none yet" in first_text

    second_text = text_of(caller.calls[1]["content_blocks"])
    assert "Screens identified so far" in second_text
    assert "S01 Screen at frame 0" in second_text
    assert "S04 Screen at frame 3" in second_text
    assert "Frame 4 at 00:40" in second_text


def test_extract_end_to_end_writes_consistent_analysis(tmp_path: Path) -> None:
    recording = make_recording(tmp_path)
    caller = FakeCaller()

    analysis = extract(recording, tmp_path, caller=caller, frames_per_call=4, log=lambda _: None)

    assert (tmp_path / "analysis.json").exists()
    assert (tmp_path / "chunk_readings.json").exists()
    assert len(json.loads((tmp_path / "chunk_readings.json").read_text())) == 3
    assert len(caller.calls) == 4  # three chunks + one consolidation

    reloaded = Analysis.model_validate_json((tmp_path / "analysis.json").read_text())
    assert reloaded == analysis

    assert [s.id for s in analysis.screens] == ["S01", "S02"]
    assert [f.id for f in analysis.fields] == ["F001", "F002"]
    assert [a.id for a in analysis.actions] == ["A001"]
    assert [r.id for r in analysis.requirements] == ["R001", "R002"]
    assert [c.id for c in analysis.acceptance_criteria] == ["AC001", "AC002", "AC003"]
    assert [q.id for q in analysis.questions] == ["Q001", "Q002"]
    assert [step.order for step in analysis.journey] == [1, 2]

    screen_ids = {s.id for s in analysis.screens}
    requirement_ids = {r.id for r in analysis.requirements}
    field_ids = {f.id for f in analysis.fields}
    action_ids = {a.id for a in analysis.actions}
    keyframe_indexes = {k.index for k in recording.keyframes}

    for field in analysis.fields:
        assert field.screen_id in screen_ids
        assert field.keyframe_index in keyframe_indexes
    for action in analysis.actions:
        assert action.screen_id in screen_ids
        assert action.leads_to_screen_id in screen_ids
    for step in analysis.journey:
        assert step.screen_id in screen_ids
    for requirement in analysis.requirements:
        assert requirement.screen_id in screen_ids
        assert requirement.keyframe_index in keyframe_indexes
    for criterion in analysis.acceptance_criteria:
        assert criterion.requirement_id in requirement_ids
    for question in analysis.questions:
        assert question.screen_id in screen_ids
    for screen in analysis.screens:
        assert set(screen.field_ids) <= field_ids
        assert set(screen.action_ids) <= action_ids
        assert set(screen.keyframe_indexes) <= keyframe_indexes
    # Screen lists agree with the fields and actions themselves.
    assert analysis.screens[0].field_ids == ["F001"]
    assert analysis.screens[0].action_ids == ["A001"]
    assert analysis.screens[1].field_ids == ["F002"]

    assert analysis.usage is not None
    assert analysis.usage.model == "fake"
    assert analysis.usage.calls == 4
    assert analysis.usage.input_tokens > 0
    assert analysis.usage.output_tokens == 4 * 300
    assert analysis.usage.cache_read_input_tokens == 3 * 800


def test_extract_reuses_existing_analysis(tmp_path: Path) -> None:
    recording = make_recording(tmp_path)
    first = extract(recording, tmp_path, caller=FakeCaller(), log=lambda _: None)

    caller = FakeCaller()
    second = extract(recording, tmp_path, caller=caller, force=False, log=lambda _: None)
    assert caller.calls == []
    assert second == first

    caller = FakeCaller()
    extract(recording, tmp_path, caller=caller, force=True, log=lambda _: None)
    assert len(caller.calls) > 0


def test_claude_caller_builds_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic

    recorded: dict = {}
    reading = ChunkReading()

    class StubMessages:
        def parse(self, **kwargs):
            recorded.update(kwargs)
            return SimpleNamespace(
                parsed_output=reading,
                stop_reason="end_turn",
                usage=SimpleNamespace(
                    input_tokens=1200,
                    output_tokens=50,
                    cache_read_input_tokens=1000,
                    cache_creation_input_tokens=0,
                ),
            )

    class StubClient:
        def __init__(self, *args, **kwargs):
            self.messages = StubMessages()

    monkeypatch.setattr(anthropic, "Anthropic", StubClient)

    caller = ClaudeCaller()
    content = [
        {"type": "text", "text": "Frame 0 at 00:00"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}},
        {"type": "text", "text": "Transcript while frame 0 was showing: hello"},
    ]
    parsed, usage = caller("SYSTEM", content, ChunkReading)

    assert parsed is reading
    assert usage == {
        "input_tokens": 1200,
        "output_tokens": 50,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 0,
    }
    assert recorded["model"] == "claude-opus-5"
    assert recorded["max_tokens"] == 16000
    assert recorded["output_format"] is ChunkReading
    assert recorded["output_config"] == {"effort": "high"}
    assert "thinking" not in recorded
    assert "temperature" not in recorded
    assert recorded["system"] == [
        {"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}
    ]
    message = recorded["messages"][0]
    assert message["role"] == "user"
    types = [b["type"] for b in message["content"]]
    assert types.index("image") < types.index("text", types.index("image"))
    assert message["content"] is content


def test_claude_caller_raises_on_refusal_and_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic

    stop = {"reason": "refusal"}

    class StubMessages:
        def parse(self, **kwargs):
            return SimpleNamespace(parsed_output=None, stop_reason=stop["reason"], stop_details=None, usage=None)

    class StubClient:
        def __init__(self, *args, **kwargs):
            self.messages = StubMessages()

    monkeypatch.setattr(anthropic, "Anthropic", StubClient)
    caller = ClaudeCaller()
    with pytest.raises(extract_module.ExtractionError, match="refused"):
        caller("SYSTEM", [{"type": "text", "text": "x"}], ChunkReading)
    stop["reason"] = "max_tokens"
    with pytest.raises(extract_module.ExtractionError, match="cut off"):
        caller("SYSTEM", [{"type": "text", "text": "x"}], ChunkReading)


def test_long_transcript_splits_consolidation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from specto.extract import RequirementsResponse, StructureResponse

    monkeypatch.setattr(extract_module, "LONG_TRANSCRIPT_CHARS", 10)
    recording = make_recording(tmp_path)
    caller = FakeCaller()

    analysis = extract(recording, tmp_path, caller=caller, frames_per_call=5, log=lambda _: None)

    models = [c["output_model"] for c in caller.calls]
    assert models == [ChunkReading, ChunkReading, RequirementsResponse, StructureResponse]
    assert [r.id for r in analysis.requirements] == ["R001", "R002"]
    assert [c.requirement_id for c in analysis.acceptance_criteria] == ["R001", "R001", "R002"]
    assert analysis.title == "Fake walkthrough"
    assert analysis.usage is not None and analysis.usage.calls == 4
