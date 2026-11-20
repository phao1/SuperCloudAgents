"""Summarize duty minutes for the payroll preview."""

from collections import Counter, defaultdict

from . import clock, schedule
from .errors import DeskError
from .intervals import elapsed
from .model import Assignment, PayrollLine, State


def report(state: State, first: str, last: str, venue: str | None = None) -> list[PayrollLine]:
    clock.days(first, last)
    if venue is not None and venue not in state.venues:
        raise DeskError("unknown venue")
    breaks = defaultdict(list)
    for row in state.breaks:
        breaks[Assignment(row.shift, row.member)].append(row)
    minutes = Counter()
    for key, shift in schedule.assigned(state):
        if not first <= shift.day <= last or (venue is not None and shift.venue != venue):
            continue
        paid = elapsed(shift.start, shift.end) - sum(row.minutes for row in breaks[key])
        minutes[shift.day, shift.venue, key.member] += max(0, paid)
    return [PayrollLine(*key, amount) for key, amount in sorted(minutes.items()) if amount]
