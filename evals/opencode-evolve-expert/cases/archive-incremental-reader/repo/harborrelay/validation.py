"""Validation stays at the wire and service boundaries."""

import re

from .errors import RelayError

SEGMENT = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")


def label(value: str, field: str) -> str:
    if not isinstance(value, str) or SEGMENT.fullmatch(value) is None:
        raise RelayError(f"invalid {field}")
    return value


def parts(value: str, *, field: str = "stream", wild: bool = False) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise RelayError(f"invalid {field}")
    segments = tuple(value.split("."))
    for segment in segments:
        if not (wild and segment == "*"):
            label(segment, field)
    return segments


def sequence(value: int, *, minimum: int = 0, field: str = "sequence") -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise RelayError(f"invalid {field}")
    return value


def key(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 100 or "\0" in value:
        raise RelayError("invalid key")
    return value
