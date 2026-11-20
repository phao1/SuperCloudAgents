"""Normalization used by the commands and file interfaces."""

import re
from datetime import date

from .errors import LedgerError

ACCOUNT = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,23}")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def code(value: str) -> str:
    if not isinstance(value, str) or not ACCOUNT.fullmatch(value):
        raise LedgerError("invalid account code")
    return value.upper()


def day(value: str) -> str:
    try:
        if isinstance(value, str) and DATE.fullmatch(value):
            return date.fromisoformat(value).isoformat()
    except ValueError:
        pass
    raise LedgerError("invalid day")


def window(first: str, last: str) -> tuple[str, str]:
    first, last = day(first), day(last)
    if first > last:
        raise LedgerError("invalid date window")
    return first, last


def label(value: str, noun: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 300 or "\0" in value:
        raise LedgerError(f"invalid {noun}")
    return value


def integer(value: int, noun: str, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= 10**9:
        raise LedgerError(f"invalid {noun}")
    return value
