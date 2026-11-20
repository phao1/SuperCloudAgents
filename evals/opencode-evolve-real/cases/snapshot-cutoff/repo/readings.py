from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime

from keys import instant_key


@dataclass(frozen=True)
class Reading:
    name: str
    value: int
    at: datetime


def before(readings: list[Reading], cutoff: datetime) -> list[Reading]:
    """Return eligible readings in chronological order without changing the input."""
    indexed = sorted(((instant_key(row.at), row) for row in readings), key=lambda item: item[0])
    width = bisect_left([key for key, _ in indexed], instant_key(cutoff))
    return [row for _, row in indexed[:width]]
