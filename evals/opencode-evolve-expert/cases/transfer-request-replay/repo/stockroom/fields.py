"""Validation shared by command, file import, and service entry points."""

import re
from datetime import date

from .errors import StockroomError

CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,23}")
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
SQLITE_MAX_INT = 2**63 - 1


def code(value: str, noun: str) -> str:
    if not isinstance(value, str) or not CODE.fullmatch(value):
        raise StockroomError(f"invalid {noun}")
    return value.upper()


def optional_code(value: str | None, noun: str) -> str | None:
    return None if value is None else code(value, noun)


def title(value: str, noun: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100:
        raise StockroomError(f"invalid {noun}")
    return value.strip()


def memo(value: str) -> str:
    if not isinstance(value, str) or len(value) > 400:
        raise StockroomError("invalid memo")
    return value


def day(value: str) -> str:
    if not isinstance(value, str) or not DAY.fullmatch(value):
        raise StockroomError("invalid day")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise StockroomError("invalid day") from None
    return value


def optional_day(value: str | None) -> str | None:
    return None if value is None else day(value)


def positive(value: int) -> int:
    if type(value) is not int or not 0 < value <= SQLITE_MAX_INT:
        raise StockroomError("invalid quantity")
    return value


def signed(value: int) -> int:
    if type(value) is not int or not -SQLITE_MAX_INT <= value <= SQLITE_MAX_INT or value == 0:
        raise StockroomError("invalid adjustment")
    return value


def active(value: bool) -> bool:
    if type(value) is not bool:
        raise StockroomError("invalid active flag")
    return value


def order_days(since: str | None, through: str | None) -> tuple[str | None, str | None]:
    first, last = optional_day(since), optional_day(through)
    if first is not None and last is not None and first > last:
        raise StockroomError("invalid day range")
    return first, last
