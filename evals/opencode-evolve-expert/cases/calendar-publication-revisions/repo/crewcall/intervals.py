"""Closed-open absolute intervals used by reports."""

from datetime import datetime


def intersect(a: tuple[datetime, datetime],
              b: tuple[datetime, datetime]) -> tuple[datetime, datetime] | None:
    start, end = max(a[0], b[0]), min(a[1], b[1])
    return (start, end) if start < end else None


def elapsed(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)
