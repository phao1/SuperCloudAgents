"""Calendar values are normalized where the schedule accepts input."""

import base64
import io
import json
import re
from datetime import UTC, date, datetime, time
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import DeskError

DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
CLOCK = re.compile(r"[0-9]{2}:[0-9]{2}")
INSTANT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?(?:Z|\+00:00)"
)


@cache
def zones_on_disk() -> dict[str, str]:
    return json.loads(Path(__file__).with_name("zone_data.json").read_text(encoding="ascii"))


@cache
def zone(name: str) -> ZoneInfo:
    try:
        if name in zones_on_disk():
            return ZoneInfo.from_file(io.BytesIO(base64.b64decode(zones_on_disk()[name])), name)
        return ZoneInfo(name)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise DeskError("unknown time zone") from None


def day(value: str) -> date:
    try:
        if isinstance(value, str) and DAY.fullmatch(value):
            return date.fromisoformat(value)
    except ValueError:
        pass
    raise DeskError("invalid date")


def wall(value: str) -> time:
    try:
        if isinstance(value, str) and CLOCK.fullmatch(value):
            return time.fromisoformat(value)
    except ValueError:
        pass
    raise DeskError("invalid clock time")


def instant(value: str) -> datetime:
    try:
        if isinstance(value, str) and INSTANT.fullmatch(value):
            return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        pass
    raise DeskError("invalid UTC instant")


def days(first: str, last: str) -> tuple[date, date]:
    start, end = day(first), day(last)
    if end < start or (end - start).days > 366:
        raise DeskError("invalid reporting range")
    return start, end


def attach(value: date, begins: str, location: str) -> datetime:
    """Convert a venue's nominal wall reading to an absolute instant."""
    return datetime.combine(value, wall(begins), zone(location)).astimezone(UTC)
