from dataclasses import dataclass


@dataclass(frozen=True)
class Segment:
    start: int
    stop: int
    label: str | None
    priority: int = 0


def compact(events, start: int, stop: int, minimum: int = 1) -> list[tuple[int, int, str]]:
    pieces = []
    for event in sorted(events, key=lambda event: event.start):
        lo, hi = max(start, event.start), min(stop, event.stop)
        if event.label and hi - lo >= minimum:
            pieces.append((lo, hi, event.label))
    return pieces
