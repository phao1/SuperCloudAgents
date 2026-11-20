"""Register work on an existing shift and record unpaid relief."""

from dataclasses import replace

from . import schedule
from .errors import DeskError
from .model import Assignment, Break, State
from .settings import count


def add(state: State, shift: str, member: str) -> tuple[State, Assignment]:
    duty = schedule.get(state, shift)
    crew = state.members.get(member)
    if crew is None:
        raise DeskError("unknown member")
    if duty.role not in crew.roles:
        raise DeskError("member lacks shift role")
    key = Assignment(shift, member)
    if key in state.assignments:
        raise DeskError("already assigned")
    return replace(state, assignments=tuple(sorted((*state.assignments, key)))), key


def remove(state: State, shift: str, member: str) -> tuple[State, Assignment]:
    key = Assignment(shift, member)
    if key not in state.assignments:
        raise DeskError("unknown assignment")
    remaining = tuple(row for row in state.assignments if row != key)
    breaks = tuple(row for row in state.breaks if (row.shift, row.member) != (shift, member))
    return replace(state, assignments=remaining, breaks=breaks), key


def crew(state: State, shift: str) -> list[str]:
    schedule.get(state, shift)
    return sorted(row.member for row in state.assignments if row.shift == shift)


def record_break(state: State, shift: str, member: str, offset: int,
                 minutes: int) -> tuple[State, Break]:
    duty = schedule.get(state, shift)
    if Assignment(shift, member) not in state.assignments:
        raise DeskError("unknown assignment")
    length = int((duty.end - duty.start).total_seconds() // 60)
    offset = count(offset, 0, length, "break offset")
    minutes = count(minutes, 1, length - offset, "break duration")
    entry = Break(shift, member, offset, minutes)
    return replace(state, breaks=tuple(sorted((*state.breaks, entry)))), entry
