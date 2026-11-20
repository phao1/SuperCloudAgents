from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Sample:
    station: str
    tick: int
    value: int | None


def normalize(samples: Iterable[Sample]) -> list[Sample]:
    """Keep the last sample per (station, tick), sorted by tick then winner arrival."""
    latest = {}
    for position, sample in enumerate(samples):
        latest[sample.tick] = (position, sample)
    return [sample for _, sample in sorted(latest.values(), key=lambda row: (row[1].tick, row[0]))]
