from collections.abc import Iterable

from history import Sample
from service import coverage


def panel(samples: Iterable[Sample], station: str, start: int, end: int) -> str:
    covered, weighted = coverage(samples, station, start, end)
    if not covered:
        raise RuntimeError("no station coverage")
    return f"{station}={weighted}/{covered}"
