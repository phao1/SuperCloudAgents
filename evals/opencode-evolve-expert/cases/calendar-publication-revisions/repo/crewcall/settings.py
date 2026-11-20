"""Maintain venue, lane, crew and weekly recurrence definitions."""

import re
from dataclasses import replace

from . import clock
from .errors import DeskError
from .model import Lane, Member, Override, Rule, State, Venue

CODE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,31}")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def code(value: str, noun: str) -> str:
    if not isinstance(value, str) or not CODE.fullmatch(value):
        raise DeskError(f"invalid {noun} code")
    return value


def title(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 400:
        raise DeskError("invalid display name")
    return value


def count(value: int, low: int, high: int, noun: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise DeskError(f"invalid {noun}")
    return value


def venue(state: State, identifier: str, name: str, zone: str) -> tuple[State, Venue]:
    entry = Venue(code(identifier, "venue"), title(name), zone)
    clock.zone(zone)
    return replace(state, venues={**state.venues, entry.code: entry}), entry


def member(state: State, identifier: str, name: str, roles: list[str]) -> tuple[State, Member]:
    identifier = code(identifier, "member")
    if not roles:
        raise DeskError("a member needs a role")
    entry = Member(identifier, title(name), tuple(sorted({code(role, "role") for role in roles})))
    return replace(state, members={**state.members, entry.code: entry}), entry


def lane(state: State, identifier: str, site: str, role: str, label: str) -> tuple[State, Lane]:
    if site not in state.venues:
        raise DeskError("unknown venue")
    entry = Lane(code(identifier, "lane"), site, code(role, "role"), title(label))
    return replace(state, lanes={**state.lanes, entry.code: entry}), entry


def weekdays(value: str) -> tuple[int, ...]:
    try:
        indices = [WEEKDAYS.index(part) for part in value.split(",")]
    except (ValueError, AttributeError):
        raise DeskError("invalid weekdays") from None
    if len(set(indices)) != len(indices):
        raise DeskError("invalid weekdays")
    return tuple(sorted(indices))


def rule(state: State, identifier: str, lane: str, pattern: str, start: str,
         minutes: int, first: str, through: str | None = None) -> tuple[State, Rule]:
    if lane not in state.lanes:
        raise DeskError("unknown lane")
    identifier = code(identifier, "rule")
    clock.wall(start)
    initial = clock.day(first)
    if through is not None and clock.day(through) < initial:
        raise DeskError("rule ends before it starts")
    entry = Rule(identifier, lane, weekdays(pattern), start,
                 count(minutes, 1, 2880, "duration"), first, through)
    return replace(state, rules={**state.rules, entry.code: entry}), entry


def override(state: State, identifier: str, date: str, *, skip: bool = False,
             start: str | None = None, minutes: int | None = None) -> tuple[State, Override]:
    if identifier not in state.rules:
        raise DeskError("unknown rule")
    clock.day(date)
    if skip and (start is not None or minutes is not None):
        raise DeskError("skip cannot include new hours")
    if start is not None:
        clock.wall(start)
    if minutes is not None:
        count(minutes, 1, 2880, "duration")
    entry = Override(identifier, date, skip, start, minutes)
    return replace(state, overrides={**state.overrides, f"{identifier}:{date}": entry}), entry
