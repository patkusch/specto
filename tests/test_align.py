"""Word-aware alignment: a sentence that runs across a screen change is cut at the word.

All words are hand-built; no speech-to-text runs here.
"""
from __future__ import annotations

import pytest

from specto.align import build_moments, split_segment_at
from specto.model import Keyframe, TranscriptSegment, Word


def _kf(index: int, t: float) -> Keyframe:
    return Keyframe(index=index, timestamp=t, path=f"frames/frame_{index:04d}.jpg")


def _words(start: float, texts: list[str], step: float = 0.5) -> list[Word]:
    return [Word(start=start + i * step, end=start + (i + 1) * step, text=w) for i, w in enumerate(texts)]


EIGHT = ["then", "you", "press", "Save", "and", "it", "goes", "through."]
THREE_FRAMES = [_kf(0, 0.0), _kf(1, 6.0), _kf(2, 12.0)]


def test_segment_spanning_a_keyframe_is_split_at_the_word():
    segment = TranscriptSegment(start=4.0, end=8.0, text=" ".join(EIGHT), speaker="Expert", words=_words(4.0, EIGHT))
    moments = build_moments(THREE_FRAMES, [segment], duration=18.0)
    assert [len(m.segments) for m in moments] == [1, 1, 0]
    head, tail = moments[0].segments[0], moments[1].segments[0]
    assert head.text == "then you press Save"
    assert tail.text == "and it goes through."
    assert (head.start, head.end) == (4.0, 6.0)
    assert (tail.start, tail.end) == (6.0, 8.0)
    assert [w.text for w in head.words] == EIGHT[:4]
    assert [w.text for w in tail.words] == EIGHT[4:]
    assert head.speaker == tail.speaker == "Expert"
    assert moments[0].text == "then you press Save"
    # The caller's segment is untouched: the transcript sheet still shows the whole sentence.
    assert segment.text == " ".join(EIGHT) and len(segment.words) == 8


def test_segment_without_words_uses_the_midpoint():
    segment = TranscriptSegment(start=4.0, end=7.0, text="no word times here")  # midpoint 5.5
    moments = build_moments(THREE_FRAMES, [segment], duration=18.0)
    assert [len(m.segments) for m in moments] == [1, 0, 0]
    assert moments[0].segments[0] is segment


def test_words_all_before_the_boundary_are_not_split():
    # The segment's end time runs past 6.0, but every word starts before it.
    words = _words(4.0, ["all", "said", "on", "the", "first"], step=0.3)
    segment = TranscriptSegment(start=4.0, end=7.5, text="all said on the first", words=words)
    moments = build_moments(THREE_FRAMES, [segment], duration=18.0)
    assert [len(m.segments) for m in moments] == [1, 0, 0]
    assert moments[0].segments[0] is segment


def test_words_all_after_the_boundary_go_to_the_later_frame_whole():
    words = _words(6.2, ["only", "on", "the", "second"], step=0.3)
    segment = TranscriptSegment(start=5.0, end=8.0, text="only on the second", words=words)
    moments = build_moments(THREE_FRAMES, [segment], duration=18.0)
    assert [len(m.segments) for m in moments] == [0, 1, 0]
    assert moments[1].segments[0] is segment


def test_one_long_segment_over_three_keyframes_gives_three_pieces():
    texts = [f"w{i}" for i in range(14)]  # 2.0 .. 16.0 in 1 s steps: crosses 6.0 and 12.0
    segment = TranscriptSegment(start=2.0, end=16.0, text=" ".join(texts), words=_words(2.0, texts, step=1.0))
    moments = build_moments(THREE_FRAMES, [segment], duration=18.0)
    assert [len(m.segments) for m in moments] == [1, 1, 1]
    pieces = [m.segments[0] for m in moments]
    assert [p.text for p in pieces] == ["w0 w1 w2 w3", "w4 w5 w6 w7 w8 w9", "w10 w11 w12 w13"]
    assert [(p.start, p.end) for p in pieces] == [(2.0, 6.0), (6.0, 12.0), (12.0, 16.0)]
    assert sum(len(p.words) for p in pieces) == 14


def test_transcript_order_is_kept_within_a_moment():
    first = TranscriptSegment(start=4.0, end=8.0, text=" ".join(EIGHT), words=_words(4.0, EIGHT))
    second = TranscriptSegment(start=8.5, end=9.5, text="next sentence", words=_words(8.5, ["next", "sentence"]))
    moments = build_moments(THREE_FRAMES, [first, second], duration=18.0)
    assert [s.text for s in moments[1].segments] == ["and it goes through.", "next sentence"]


def test_split_segment_at_directly():
    segment = TranscriptSegment(start=4.0, end=8.0, text=" ".join(EIGHT), speaker="Expert", words=_words(4.0, EIGHT))
    head, tail = split_segment_at(segment, 5.2)  # falls inside the word "press" (5.0-5.5)
    assert head.text == "then you press" and (head.start, head.end) == (4.0, 5.5)
    assert tail.text == "Save and it goes through." and (tail.start, tail.end) == (5.5, 8.0)
    assert head.speaker == tail.speaker == "Expert"

    head, tail = split_segment_at(segment, 3.0)  # before every word
    assert head.text == "" and head.words == [] and (head.start, head.end) == (3.0, 3.0)
    assert tail.text == " ".join(EIGHT) and len(tail.words) == 8

    with pytest.raises(ValueError, match="word timings"):
        split_segment_at(TranscriptSegment(start=0.0, end=1.0, text="no words"), 0.5)


def test_words_default_to_empty_so_old_recordings_still_load():
    old = TranscriptSegment.model_validate({"start": 1.0, "end": 2.0, "text": "from an older recording.json"})
    assert old.words == []
    assert old.model_dump()["words"] == []
