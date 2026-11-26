from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

PARK_TZ = timezone(timedelta(hours=8))
PARK_DATE = "2026-12-03"


def parse_minute(value, field: str = "time") -> int:
    """Parse HH:MM/HH:MM:SS or an ISO timestamp into park-day minutes."""
    if value is None:
        raise ValueError(f"{field} is required")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minute = int(value)
        if 0 <= minute <= 1440:
            return minute
        raise ValueError(f"{field} minute must be between 0 and 1440")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} is required")
    if "T" in text or "-" in text and ":" in text:
        normalized = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"invalid {field}: {value!r}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=PARK_TZ)
        dt = dt.astimezone(PARK_TZ)
        # The exchange uses clock labels for the single park day.  Keeping the
        # minute-of-day makes interval comparisons independent of wire date.
        return dt.hour * 60 + dt.minute
    parts = text.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"invalid {field}: {value!r}")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc
    total = hour * 60 + minute
    if second >= 30:
        total += 1
    if not (0 <= total <= 1440):
        raise ValueError(f"invalid {field}: {value!r}")
    return total


def format_minute(value: int | None) -> str | None:
    if value is None:
        return None
    value = int(value)
    if value == 1440:
        return "24:00"
    return f"{value // 60:02d}:{value % 60:02d}"


def normalize_interval(start, end, field: str = "interval") -> tuple[int, int]:
    s = parse_minute(start, f"{field}.start")
    e = parse_minute(end, f"{field}.end")
    if e <= s:
        raise ValueError(f"{field} end must be after start")
    return s, e


def overlap_minutes(a: Sequence[int], b: Sequence[int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def intervals_overlap(a: Sequence[int], b: Sequence[int]) -> bool:
    return overlap_minutes(a, b) > 0


def merge_intervals(intervals: Iterable[Sequence[int]]) -> list[tuple[int, int]]:
    ordered = sorted((int(s), int(e)) for s, e in intervals if e > s)
    merged: list[list[int]] = []
    for s, e in ordered:
        if not merged or s > merged[-1][1]:
            merged.append([s, e])
        else:
            merged[-1][1] = max(merged[-1][1], e)
    return [(s, e) for s, e in merged]


def subtract_intervals(source: Sequence[int], cuts: Iterable[Sequence[int]]) -> list[tuple[int, int]]:
    """Return source pieces after removing every cut interval."""
    pieces = [(int(source[0]), int(source[1]))]
    for cs, ce in merge_intervals(cuts):
        next_pieces: list[tuple[int, int]] = []
        for s, e in pieces:
            if ce <= s or cs >= e:
                next_pieces.append((s, e))
                continue
            if cs > s:
                next_pieces.append((s, min(cs, e)))
            if ce < e:
                next_pieces.append((max(ce, s), e))
        pieces = next_pieces
    return merge_intervals(pieces)


def intersect_intervals(a: Sequence[int], b: Sequence[int]) -> tuple[int, int] | None:
    s = max(int(a[0]), int(b[0]))
    e = min(int(a[1]), int(b[1]))
    return (s, e) if e > s else None


def round_cents_half_up(numerator: int, denominator: int = 60) -> int:
    """Round numerator/denominator to an integer number of cents, half up."""
    if numerator < 0:
        return -round_cents_half_up(-numerator, denominator)
    return (numerator + denominator // 2) // denominator
