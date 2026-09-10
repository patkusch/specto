"""Turn seconds into the mm:ss text used in the spreadsheet, the report and frame labels."""
from __future__ import annotations


def mmss(seconds: float) -> str:
    """Format seconds as mm:ss, or h:mm:ss once the time passes an hour.

    65 -> "01:05", 3725 -> "1:02:05". Fractions of a second are dropped.
    Negative or missing values are treated as zero.
    """
    total = int(seconds) if seconds and seconds > 0 else 0
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
