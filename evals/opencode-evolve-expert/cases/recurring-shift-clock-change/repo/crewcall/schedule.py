"""Build effective shifts from weekly rules and operator exceptions."""

from datetime import date, timedelta

from . import clock
from .errors import DeskError
from .model import Rule, Shift, State


def materialize(state: State, rule: Rule, day: date) -> Shift | None:
    label = day.isoformat()
    if label < rule.first or (rule.through and label > rule.through):
        return None
    if day.weekday() not in rule.weekdays:
        return None
    identity = f"{rule.code}:{label}"
    alternative = state.overrides.get(identity)
    if alternative and alternative.skip:
        return None
    begins = alternative.start if alternative and alternative.start is not None else rule.start
    duration = alternative.minutes if alternative and alternative.minutes is not None else rule.minutes
    lane = state.lanes[rule.lane]
    site = state.venues[lane.venue]
    start = clock.attach(day, begins, site.zone)
    return Shift(identity, rule.code, label, lane.code, lane.venue, lane.role, lane.label,
                 start, start + timedelta(minutes=duration))


def get(state: State, identity: str) -> Shift:
    rule, separator, date = identity.partition(":")
    if not separator or rule not in state.rules:
        raise DeskError("unknown shift")
    shift = materialize(state, state.rules[rule], clock.day(date))
    if shift is None:
        raise DeskError("unknown shift")
    return shift


def order(shift: Shift) -> tuple:
    return shift.start, shift.lane, shift.id


def between(state: State, first: str, last: str, venue: str | None = None) -> list[Shift]:
    start, end = clock.days(first, last)
    if venue is not None and venue not in state.venues:
        raise DeskError("unknown venue")
    rules = [rule for rule in state.rules.values()
             if venue is None or state.lanes[rule.lane].venue == venue]
    rows = []
    for offset in range((end - start).days + 1):
        date = start + timedelta(days=offset)
        for rule in rules:
            shift = materialize(state, rule, date)
            if shift is not None:
                rows.append(shift)
    return sorted(rows, key=order)


def assigned(state: State, member: str | None = None):
    if member is not None and member not in state.members:
        raise DeskError("unknown member")
    for binding in state.assignments:
        if member is not None and binding.member != member:
            continue
        try:
            shift = get(state, binding.shift)
        except DeskError:
            continue
        yield binding, shift
