"""Pair each still frame with what was said while it was on screen.

A keyframe owns the time from its own timestamp up to the next keyframe's
timestamp (the last one runs to the end of the recording). A transcript segment
that has word timings is placed word by word: the words spoken before a screen
change go with the earlier frame and the rest with the later one, so a sentence
that runs across a screen change is cut at the right word. A segment without
word timings goes whole to whichever keyframe owns its midpoint, which is off by
at most half a sentence at a screen change.
"""
from __future__ import annotations

from bisect import bisect_right

from specto.model import Keyframe, Moment, TranscriptSegment


def build_moments(keyframes: list[Keyframe], segments: list[TranscriptSegment], duration: float) -> list[Moment]:
    """Return one Moment per keyframe, in keyframe order, with its segments.

    Every keyframe gets a Moment even when nobody spoke during it, so the
    extractor still sees the screen. Segments are kept in transcript order. A
    segment with words that crosses one or more keyframe boundaries is split
    there; the caller's list is not changed, only the moments hold the pieces.
    """
    if not keyframes:
        return []
    ordered = sorted(keyframes, key=lambda k: k.timestamp)
    starts = [k.timestamp for k in ordered]
    moments: list[Moment] = []
    for i, keyframe in enumerate(ordered):
        end = starts[i + 1] if i + 1 < len(ordered) else max(duration, keyframe.timestamp)
        moments.append(Moment(keyframe_index=keyframe.index, start=keyframe.timestamp, end=end))

    def slot_for(t: float) -> int:
        return min(max(bisect_right(starts, t) - 1, 0), len(moments) - 1)

    splits = 0
    for segment in segments:
        if not segment.words:
            moments[slot_for((segment.start + segment.end) / 2)].segments.append(segment)
            continue
        piece = segment
        while True:
            slot = slot_for(piece.words[0].start)
            boundary = starts[slot + 1] if slot + 1 < len(starts) else None
            if boundary is None or not any(w.start >= boundary for w in piece.words):
                moments[slot].segments.append(piece)
                break
            head, piece = split_segment_at(piece, boundary)
            moments[slot].segments.append(head)
            splits += 1

    spoken = sum(1 for m in moments if m.segments)
    note = f", {splits} split at a screen change" if splits else ""
    print(f"align: {len(segments)} segments placed on {len(moments)} keyframes ({spoken} with speech{note})")
    return moments


def split_segment_at(segment: TranscriptSegment, t: float) -> tuple[TranscriptSegment, TranscriptSegment]:
    """Cut a segment with word timings into the part before `t` and the part after.

    A word belongs to the first part when it starts before `t`. Each part's text
    is its words joined with spaces; its start and end come from the original
    segment on the outside and from its own first or last word at the cut. The
    speaker is kept on both. When every word falls on one side, the other part
    is empty (no text, no words, zero length at `t`).
    """
    if not segment.words:
        raise ValueError("split_segment_at needs a segment with word timings")
    before = [w for w in segment.words if w.start < t]
    after = [w for w in segment.words if w.start >= t]
    head = TranscriptSegment(
        start=segment.start,
        end=before[-1].end if before else segment.start,
        text=" ".join(w.text for w in before),
        speaker=segment.speaker,
        words=before,
    )
    tail = TranscriptSegment(
        start=after[0].start if after else segment.end,
        end=segment.end,
        text=" ".join(w.text for w in after),
        speaker=segment.speaker,
        words=after,
    )
    if not before:
        head = TranscriptSegment(start=t, end=t, text="", speaker=segment.speaker)
    if not after:
        tail = TranscriptSegment(start=t, end=t, text="", speaker=segment.speaker)
    return head, tail
