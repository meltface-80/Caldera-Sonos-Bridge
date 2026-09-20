"""Duration string helpers shared by the transport logic."""

from __future__ import annotations


def to_seconds(value: str) -> float:
    """Parse ``H:MM:SS[.mmm]`` (or ``MM:SS``) into seconds.  ``-1`` if unusable."""
    text = (value or "").strip()
    if not text or text.upper() in ("NOT_IMPLEMENTED", "NOTIMPLEMENTED"):
        return -1.0
    parts = text.split(":")
    if len(parts) > 3:
        return -1.0
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return -1.0
    total = 0.0
    for number in numbers:
        total = total * 60 + number
    return total
