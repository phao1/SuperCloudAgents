from collections.abc import Iterable

from history import Sample
from window import measure


def coverage(samples: Iterable[Sample], station: str, start: int, end: int) -> tuple[int, int]:
    return measure(samples, station, start, end)
