"""Advisory conflicts among one member's assigned shifts."""

from itertools import combinations

from . import clock, schedule
from .errors import DeskError
from .intervals import intersect
from .model import Conflict, State


def conflicts(state: State, member: str, since: str, through: str) -> list[Conflict]:
    low, high = clock.instant(since), clock.instant(through)
    if low >= high:
        raise DeskError("invalid review window")
    selected = [shift for _, shift in schedule.assigned(state, member) if low <= shift.start < high]
    result = []
    for left, right in combinations(sorted(selected, key=schedule.order), 2):
        overlap = intersect((left.start, left.end), (right.start, right.end))
        if overlap is not None:
            result.append(Conflict(member, left.id, right.id, *overlap))
    return result
