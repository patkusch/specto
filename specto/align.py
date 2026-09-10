"""Pair each still frame with what was said while it was on screen.

A keyframe owns the time from its own timestamp up to the next keyframe's
timestamp (the last one runs to the end of the recording). A transcript segment
goes to whichever keyframe owns the segment's midpoint. Midpoint only, no
splitting: it keeps the rule easy to explain and the error is at most half a
sentence at a screen change.
"""
from __future__ import annotations

from bisect import bisect_right

from specto.model import Keyframe, Moment, TranscriptSegment


def build_moments(keyframes: list[Keyframe], segments: list[TranscriptSegment], duration: float) -> list[Moment]:
    """Return one Moment per keyframe, in keyframe order, with its segments.

    Every keyframe gets a Moment even when nobody spoke during it, so the
    extractor still sees the screen. Segments are kept in transcript order.
    """
    if not keyframes:
        return []
    ordered = sorted(keyframes, key=lambda k: k.timestamp)
    starts = [k.timestamp for k in ordered]
    moments: list[Moment] = []
    for i, keyframe in enumerate(ordered):
        end = starts[i + 1] if i + 1 < len(ordered) else max(duration, keyframe.timestamp)
        moments.append(Moment(keyframe_index=keyframe.index, start=keyframe.timestamp, end=end))

    for segment in segments:
        midpoint = (segment.start + segment.end) / 2
        slot = bisect_right(starts, midpoint) - 1
        slot = min(max(slot, 0), len(moments) - 1)
        moments[slot].segments.append(segment)

    spoken = sum(1 for m in moments if m.segments)
    print(f"align: {len(segments)} segments placed on {len(moments)} keyframes ({spoken} with speech)")
    return moments
