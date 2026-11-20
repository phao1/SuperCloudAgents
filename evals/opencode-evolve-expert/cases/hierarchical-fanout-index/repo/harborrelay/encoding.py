"""One canonical JSON projection for file and command output."""

import json
from dataclasses import asdict
from typing import Any

from .errors import RelayError
from .types import Envelope, Notice
from .validation import key, label, parts, sequence

FIELDS = {"seq", "stream", "kind", "key", "value", "stamp"}


def as_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise RelayError("invalid JSON value") from None


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for name, item in pairs:
        if name in value:
            raise RelayError("duplicate JSON field")
        value[name] = item
    return value


def _nonfinite(_: str) -> None:
    raise RelayError("invalid JSON value")


def decode(value: str | bytes) -> Any:
    try:
        data = value.decode("utf-8") if isinstance(value, bytes) else value
        return json.loads(data, object_pairs_hook=_unique, parse_constant=_nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        raise RelayError("invalid JSON value") from None


def validate(notice: Notice) -> Notice:
    parts(notice.stream)
    label(notice.kind, "kind")
    key(notice.key)
    sequence(notice.stamp, field="stamp")
    if not isinstance(notice.value, dict):
        raise RelayError("invalid payload")
    value = decode(as_bytes(notice.value))
    return Notice(notice.stream, notice.kind, notice.key, value, notice.stamp)


def envelope(raw: Any) -> Envelope:
    if not isinstance(raw, dict) or raw.keys() != FIELDS:
        raise RelayError("invalid journal record")
    identity = sequence(raw["seq"], minimum=1)
    notice = validate(Notice(raw["stream"], raw["kind"], raw["key"], raw["value"], raw["stamp"]))
    return Envelope(identity, notice.stream, notice.kind, notice.key, notice.value, notice.stamp)


def row_bytes(row: Envelope) -> bytes:
    return as_bytes(asdict(row))
