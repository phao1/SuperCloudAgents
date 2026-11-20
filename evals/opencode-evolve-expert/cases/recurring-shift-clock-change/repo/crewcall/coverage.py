"""Find uncovered lanes and flag assignments that need desk review."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from . import schedule
from .intervals import elapsed
from .model import State


@dataclass(frozen=True)
class Coverage:
    shift: str
    day: str
    venue: str
    lane: str
    role: str
    start: datetime
    end: datetime
    crew: tuple[str, ...]
    review: tuple[str, ...]

    @property
    def covered(self) -> bool:
        return bool(self.crew)


@dataclass(frozen=True)
class Gap:
    day: str
    venue: str
    role: str
    shifts: tuple[str, ...]
    minutes: int


def board(state: State, first: str, last: str, venue: str | None = None) -> list[Coverage]:
    assigned: dict[str, list[str]] = defaultdict(list)
    for row in state.assignments:
        assigned[row.shift].append(row.member)
    result = []
    for row in schedule.between(state, first, last, venue):
        active, review = [], []
        for code in sorted(assigned[row.id]):
            member = state.members.get(code)
            if member is not None and row.role in member.roles:
                active.append(code)
            else:
                review.append(code)
        result.append(Coverage(row.id, row.day, row.venue, row.lane, row.role, row.start,
                               row.end, tuple(active), tuple(review)))
    return result


def gaps(state: State, first: str, last: str, venue: str | None = None) -> list[Gap]:
    missing: dict[tuple[str, str, str], list[Coverage]] = defaultdict(list)
    for row in board(state, first, last, venue):
        if not row.covered:
            missing[row.day, row.venue, row.role].append(row)
    return [
        Gap(*identity, tuple(row.shift for row in rows),
            sum(elapsed(row.start, row.end) for row in rows))
        for identity, rows in sorted(missing.items())
    ]
