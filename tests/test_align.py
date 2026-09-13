"""Word-aware alignment: a sentence that runs across a screen change is cut at the word.

All words are hand-built; no speech-to-text runs here. Sentences without word
times (transcript files) get estimated times first, spread by word length.
"""
from __future__ import annotations

import pytest

from specto.align import build_moments, estimate_words, split_segment_at
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


def test_segment_without_words_uses_the_midpoint_when_estimation_is_off():
    segment = TranscriptSegment(start=4.0, end=7.0, text="no word times here")  # midpoint 5.5
    moments = build_moments(THREE_FRAMES, [segment], duration=18.0, estimate_word_times=False)
    assert [len(m.segments) for m in moments] == [1, 0, 0]
    assert moments[0].segments[0] is segment


# -- estimated word times for transcript-file sentences ------------------------

EIGHT_EQUAL = ["then", "you.", "push", "save", "and.", "then", "it's", "done"]  # 4 characters each


def test_segment_without_words_crossing_a_boundary_is_split_by_estimated_times():
    segment = TranscriptSegment(start=4.0, end=8.0, text=" ".join(EIGHT_EQUAL), speaker="Expert")
    moments = build_moments(THREE_FRAMES, [segment], duration=18.0)
    assert [len(m.segments) for m in moments] == [1, 1, 0]
    head, tail = moments[0].segments[0], moments[1].segments[0]
    # Equal-length words over 4 s cross 6.0 exactly halfway: four words each side.
    assert head.text == "then you. push save"
    assert tail.text == "and. then it's done"
    assert (head.start, head.end) == (4.0, 6.0)
    assert (tail.start, tail.end) == (6.0, 8.0)
    assert head.speaker == tail.speaker == "Expert"
    assert [w.text for w in head.words] == EIGHT_EQUAL[:4]
    assert [w.text for w in tail.words] == EIGHT_EQUAL[4:]
    # The caller's segment keeps its sentence and still has no words.
    assert segment.text == " ".join(EIGHT_EQUAL) and segment.words == []


def test_eight_words_of_mixed_length_split_about_half_and_half():
    segment = TranscriptSegment(start=4.0, end=8.0, text=" ".join(EIGHT))
    moments = build_moments(THREE_FRAMES, [segment], duration=18.0)
    assert [len(m.segments) for m in moments] == [1, 1, 0]
    head, tail = moments[0].segments[0], moments[1].segments[0]
    assert 3 <= len(head.words) <= 5 and len(head.words) + len(tail.words) == 8
    assert head.text + " " + tail.text == " ".join(EIGHT)
    # As with real words, a word goes before the change when it starts before it.
    assert head.words[-1].start < 6.0 <= tail.words[0].start


def test_unequal_word_lengths_shift_the_split_proportionally():
    # 30 characters over 4 s. The two long words (26 of them) both start before
    # 6.0, so they go first; the four short words all start after it.
    front_heavy = TranscriptSegment(start=4.0, end=8.0, text="abcdefghij klmnopqrstuvwxyz a b c d")
    moments = build_moments(THREE_FRAMES, [front_heavy], duration=18.0)
    assert [len(m.segments) for m in moments] == [1, 1, 0]
    head, tail = moments[0].segments[0], moments[1].segments[0]
    assert head.text == "abcdefghij klmnopqrstuvwxyz"
    assert tail.text == "a b c d"
    # The mirror image: four short words then one long one. Every word starts
    # before 6.0, so nothing is split and the sentence stays on the first frame,
    # where the midpoint rule (6.0 -> second frame) would have moved it.
    back_heavy = TranscriptSegment(start=4.0, end=8.0, text="a b c d abcdefghijklmnopqrstuvwxyz")
    moments = build_moments(THREE_FRAMES, [back_heavy], duration=18.0)
    assert [len(m.segments) for m in moments] == [1, 0, 0]
    assert moments[0].segments[0] is back_heavy and back_heavy.words == []


def test_estimation_off_restores_the_midpoint_rule():
    segment = TranscriptSegment(start=4.0, end=8.0, text=" ".join(EIGHT_EQUAL))  # midpoint 6.0 -> second frame
    moments = build_moments(THREE_FRAMES, [segment], duration=18.0, estimate_word_times=False)
    assert [len(m.segments) for m in moments] == [0, 1, 0]
    assert moments[1].segments[0] is segment and segment.words == []


def test_segment_inside_one_interval_is_the_same_object_with_no_words():
    inside = TranscriptSegment(start=6.5, end=9.0, text="all on the second screen")
    ends_on_boundary = TranscriptSegment(start=0.0, end=6.0, text="runs right up to the change")
    starts_on_boundary = TranscriptSegment(start=12.0, end=15.0, text="starts as the screen changes")
    moments = build_moments(THREE_FRAMES, [inside, ends_on_boundary, starts_on_boundary], duration=18.0)
    assert moments[0].segments == [ends_on_boundary] and moments[0].segments[0] is ends_on_boundary
    assert moments[1].segments == [inside] and moments[1].segments[0] is inside
    assert moments[2].segments == [starts_on_boundary] and moments[2].segments[0] is starts_on_boundary
    assert all(s.words == [] for m in moments for s in m.segments)


def test_segment_with_real_words_is_not_re_estimated():
    # Words all start before 6.0 even though the segment runs to 8.0: real times win, no split.
    words = _words(4.0, EIGHT, step=0.2)
    segment = TranscriptSegment(start=4.0, end=8.0, text=" ".join(EIGHT), words=words)
    moments = build_moments(THREE_FRAMES, [segment], duration=18.0)
    assert [len(m.segments) for m in moments] == [1, 0, 0]
    assert moments[0].segments[0] is segment
    assert [(w.start, w.end) for w in moments[0].segments[0].words] == [(w.start, w.end) for w in words]


def test_estimate_words_spreads_time_by_character_count():
    segment = TranscriptSegment(start=10.0, end=14.0, text="a bb, cccc")  # 1 + 3 + 4 = 8 characters over 4 s
    words = estimate_words(segment)
    assert [w.text for w in words] == ["a", "bb,", "cccc"]
    assert [(w.start, w.end) for w in words] == [(10.0, 10.5), (10.5, 12.0), (12.0, 14.0)]
    assert words[0].start == segment.start and words[-1].end == segment.end
    assert estimate_words(TranscriptSegment(start=1.0, end=2.0, text="   ")) == []
    flat = estimate_words(TranscriptSegment(start=3.0, end=3.0, text="zero length"))
    assert [(w.start, w.end) for w in flat] == [(3.0, 3.0), (3.0, 3.0)]


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
