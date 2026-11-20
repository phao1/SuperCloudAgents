from collections.abc import Iterable

from history import Sample, normalize


def measure(samples: Iterable[Sample], station: str, start: int, end: int) -> tuple[int, int]:
    selected = [sample for sample in normalize(samples) if sample.station == station]
    covered = 0
    weighted = 0
    for index, point in enumerate(selected):
        next_tick = selected[index + 1].tick if index + 1 < len(selected) else end
        width = min(next_tick, end) - max(start, point.tick)
        if point.value is not None and width > 0:
            covered += width
            weighted += width * point.value
    return covered, weighted
