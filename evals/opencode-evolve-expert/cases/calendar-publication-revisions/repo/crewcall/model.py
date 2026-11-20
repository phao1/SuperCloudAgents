"""Values in the desk file and derived operator views."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Venue:
    code: str
    name: str
    zone: str


@dataclass(frozen=True)
class Member:
    code: str
    name: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class Lane:
    code: str
    venue: str
    role: str
    label: str


@dataclass(frozen=True)
class Rule:
    code: str
    lane: str
    weekdays: tuple[int, ...]
    start: str
    minutes: int
    first: str
    through: str | None = None


@dataclass(frozen=True)
class Override:
    rule: str
    day: str
    skip: bool = False
    start: str | None = None
    minutes: int | None = None


@dataclass(frozen=True)
class Shift:
    id: str
    rule: str
    day: str
    lane: str
    venue: str
    role: str
    label: str
    start: datetime
    end: datetime


@dataclass(frozen=True, order=True)
class Assignment:
    shift: str
    member: str


@dataclass(frozen=True, order=True)
class Break:
    shift: str
    member: str
    offset: int
    minutes: int


@dataclass(frozen=True)
class PayrollLine:
    day: str
    venue: str
    member: str
    minutes: int


@dataclass(frozen=True)
class Conflict:
    member: str
    first: str
    second: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class State:
    venues: dict[str, Venue] = field(default_factory=dict)
    members: dict[str, Member] = field(default_factory=dict)
    lanes: dict[str, Lane] = field(default_factory=dict)
    rules: dict[str, Rule] = field(default_factory=dict)
    overrides: dict[str, Override] = field(default_factory=dict)
    assignments: tuple[Assignment, ...] = ()
    breaks: tuple[Break, ...] = ()
