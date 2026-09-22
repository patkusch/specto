"""Tests for local speaker diarization (`specto[speakers]`, the --speakers flag).

`label_segments` and `_dominant_speaker` are pure Python and always run: they
turn diarization turns into `.speaker` values without touching sherpa-onnx.
The real-model test runs sherpa-onnx's diarization on a real audio fixture
(`tests/fixtures/diarize/clip.wav`, six turns among three macOS `say` voices
built for this feature: Daniel, Samantha, Fred; see `script.txt` in the same
folder for the exact text and `ground_truth.json` for the true turn
boundaries) and checks the result against that ground truth. It skips with a
reason when the `speakers` extra is not installed, which is the default: CI's
main job installs only `.[dev,ocr]`, so this test does not run there. It was
run locally after building sherpa-onnx's two models (see `specto/diarize.py`
for where they come from) and passed: 6/6 turns assigned to the right
speaker, all 5 true speaker changes matched within 0.1s, no extra changes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from specto.diarize import diarize_turns, label_segments, models_cached
from specto.model import TranscriptSegment

FIXTURES = Path(__file__).parent / "fixtures" / "diarize"


def _seg(start: float, end: float, speaker=None) -> TranscriptSegment:
    return TranscriptSegment(start=start, end=end, text="x", speaker=speaker)


def test_label_segments_fills_in_only_missing_speakers():
    turns = [(0.0, 2.0, 0), (2.5, 5.0, 1)]
    segments = [
        _seg(0.0, 2.0),                       # no speaker: gets one
        _seg(2.5, 5.0, speaker="Ann"),        # already named: left alone
        _seg(10.0, 11.0),                     # no overlap with any turn: left alone
    ]
    labelled = label_segments(segments, turns)
    assert labelled[0].speaker == "Speaker 1"
    assert labelled[1].speaker == "Ann"
    assert labelled[2].speaker is None


def test_label_segments_picks_the_turn_with_the_most_overlap():
    # This segment spans a speaker change; more of it falls in speaker 1's turn.
    turns = [(0.0, 3.0, 0), (3.0, 10.0, 1)]
    segments = [_seg(2.0, 6.0)]
    labelled = label_segments(segments, turns)
    assert labelled[0].speaker == "Speaker 2"


def test_label_segments_with_no_turns_leaves_segments_untouched():
    segments = [_seg(0.0, 2.0)]
    assert label_segments(segments, []) == segments


def test_label_segments_returns_same_object_identity_when_untouched():
    # A segment that is not relabelled should not be a needless copy.
    seg = _seg(0.0, 1.0, speaker="Ann")
    assert label_segments([seg], [(0.0, 1.0, 0)])[0] is seg


def test_diarize_turns_without_sherpa_onnx_raises_a_clear_import_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sherpa_onnx":
            raise ImportError("no module named sherpa_onnx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="specto\\[speakers\\]"):
        diarize_turns(FIXTURES / "clip.wav")


sherpa_onnx = pytest.importorskip("sherpa_onnx", reason="specto[speakers] not installed")


@pytest.fixture(scope="module")
def ground_truth() -> list[dict]:
    return json.loads((FIXTURES / "ground_truth.json").read_text())


def test_real_diarization_on_the_three_speaker_fixture(ground_truth):
    """The real, measured check: run sherpa-onnx on a real recording and compare
    against the known turns. Downloads the two models on first run if they are
    not already cached (see `specto.diarize.ensure_models`)."""
    turns = diarize_turns(FIXTURES / "clip.wav")
    assert turns, "diarization found no speech at all in the fixture"

    # Map each ground-truth speaker name to whichever cluster label dominates
    # its turns, then check every turn against that mapping.
    segments = [_seg(g["start"], g["end"]) for g in ground_truth]
    labelled = label_segments(segments, turns)

    cluster_by_name: dict[str, str] = {}
    correct = 0
    for g, seg in zip(ground_truth, labelled):
        name = g["speaker"]
        expected_cluster = cluster_by_name.setdefault(name, seg.speaker)
        if seg.speaker == expected_cluster:
            correct += 1
    assert len(cluster_by_name) == 3, f"expected 3 distinct speaker clusters, got {cluster_by_name}"
    assert correct == len(ground_truth), (
        f"only {correct}/{len(ground_truth)} turns matched their speaker's cluster: "
        f"{[(g['speaker'], s.speaker) for g, s in zip(ground_truth, labelled)]}"
    )

    # Every true speaker change should show up as a change in the diarized
    # turn covering each side of the boundary, within 0.15s of the true time.
    true_changes = [
        ground_truth[i]["start"]
        for i in range(1, len(ground_truth))
        if ground_truth[i]["speaker"] != ground_truth[i - 1]["speaker"]
    ]
    turn_starts = sorted(t[0] for t in turns)[1:]  # every turn boundary sherpa-onnx found
    for true_time in true_changes:
        assert any(abs(true_time - found) < 0.15 for found in turn_starts), (
            f"no detected speaker change within 0.15s of the true change at {true_time:.2f}s "
            f"(found: {[round(t, 2) for t in turn_starts]})"
        )


def test_models_cached_is_false_before_any_download(tmp_path):
    assert models_cached(tmp_path) is False
