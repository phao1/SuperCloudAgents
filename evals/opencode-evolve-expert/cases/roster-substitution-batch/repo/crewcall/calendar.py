"""Render the operator's iCalendar snapshot."""

from collections.abc import Iterable
from datetime import datetime

from . import clock, schedule
from .model import Shift, State


def escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n")
            .replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;"))


def fold(text: str) -> str:
    lines, current, size = [], [], 0
    for character in text:
        needed = len(character.encode("utf-8"))
        if size + needed > 75:
            lines.append("".join(current))
            current, size = [" "], 1
        current.append(character)
        size += needed
    lines.append("".join(current))
    return "\r\n".join(lines)


def stamp(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def uid(shift: Shift) -> str:
    return f"{shift.id}@crewcall"


def event(shift: Shift, at: datetime) -> Iterable[str]:
    return (
        "BEGIN:VEVENT", f"UID:{uid(shift)}",
        f"DTSTAMP:{stamp(at)}", f"DTSTART:{stamp(shift.start)}",
        f"DTEND:{stamp(shift.end)}", f"SUMMARY:{escape(shift.label)}",
        "END:VEVENT",
    )


def render(events: Iterable[Iterable[str]]) -> str:
    header = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Crewcall//Roster//EN"]
    for row in events:
        header.extend(row)
    header.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in header) + "\r\n"


def export(state: State, first: str, last: str, at: str) -> str:
    time = clock.instant(at)
    return render(event(shift, time) for shift in schedule.between(state, first, last))
