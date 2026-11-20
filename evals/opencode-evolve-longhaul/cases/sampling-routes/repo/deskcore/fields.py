"""Validation shared by the route dispatcher, CLI and legacy bundle importer."""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from .errors import fail

type Check = Callable[[Any], Any]
type Row = dict[str, Any]
_ABSENT = object()
KEY = re.compile(r"[a-z][a-z0-9-]{0,47}")
PERSON = re.compile(r"[a-z][a-z0-9_.-]{0,47}")


def key(value: Any) -> str:
    if not isinstance(value, str) or KEY.fullmatch(value) is None:
        fail("bad_request", "expected a lowercase desk identifier")
    return value


def actor_key(value: Any) -> str:
    if not isinstance(value, str) or PERSON.fullmatch(value) is None:
        fail("bad_request", "expected a lowercase actor identifier")
    return value


def text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        fail("bad_request", "expected 1–500 characters of text")
    return value.strip()


def choice(*options: str) -> Check:
    def check(value: Any) -> str:
        if not isinstance(value, str) or value not in options:
            fail("bad_request", "expected " + ", ".join(options))
        return value

    return check


def number(low: int = 0, high: int = 10_000_000) -> Check:
    def check(value: Any) -> int:
        if type(value) is not int or not low <= value <= high:
            fail("bad_request", f"expected an integer between {low} and {high}")
        return value

    return check


def day(value: Any) -> str:
    if not isinstance(value, str):
        fail("bad_request", "expected a date in YYYY-MM-DD format")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        fail("bad_request", "expected a date in YYYY-MM-DD format")
    if parsed.isoformat() != value:
        fail("bad_request", "expected a date in YYYY-MM-DD format")
    return value


def keys(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 100:
        fail("bad_request", "expected a list of 1–100 identifiers")
    result = [key(item) for item in value]
    if len(set(result)) != len(result):
        fail("bad_request", "identifiers must be unique")
    return result


def object_fields(value: Any) -> Row:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        fail("bad_request", "expected an object")
    return dict(value)


def decode_integer(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        fail("bad_request", "expected an integer")


def decode_json(value: str) -> Any:
    try:
        return json.loads(value, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        fail("bad_request", "invalid JSON argument")


def reject_constant(value: str) -> None:
    fail("bad_request", f"invalid JSON constant {value}")


@dataclass(frozen=True)
class Field:
    name: str
    check: Check
    help: str
    default: Any = _ABSENT
    cli: Callable[[str], Any] = str

    @property
    def required(self) -> bool:
        return self.default is _ABSENT

    @property
    def option(self) -> str:
        return "--" + self.name.replace("_", "-")


def id_field(name: str = "id", help: str = "record identifier") -> Field:
    return Field(name, key, help)


def text_field(name: str, help: str) -> Field:
    return Field(name, text, help)


def date_field(name: str = "day", help: str = "calendar date") -> Field:
    return Field(name, day, help)


def int_field(name: str, help: str, low: int = 0, high: int = 10_000_000) -> Field:
    return Field(name, number(low, high), help, cli=decode_integer)


def enum_field(name: str, help: str, *options: str) -> Field:
    return Field(name, choice(*options), help)


def ids_field(name: str, help: str) -> Field:
    return Field(name, keys, help, cli=decode_json)


def json_field(name: str, help: str) -> Field:
    return Field(name, object_fields, help, cli=decode_json)


def optional_id(name: str, help: str) -> Field:
    return Field(name, key, help, default=None)


def optional_text(name: str, help: str) -> Field:
    return Field(name, text, help, default=None)


def bind(fields: tuple[Field, ...], supplied: Mapping[str, Any]) -> Row:
    known = {field.name for field in fields}
    unexpected = supplied.keys() - known
    if unexpected:
        fail("bad_request", f"unknown argument: {min(unexpected)}")

    result = {}
    for field in fields:
        if field.name in supplied:
            result[field.name] = field.check(supplied[field.name])
        elif field.required:
            fail("bad_request", f"missing argument: {field.name}")
        else:
            result[field.name] = field.default
    return result


def optional_date(name: str, help: str) -> Field:
    return Field(name, day, help, default=None)


def optional_int(name: str, help: str, low: int = 0) -> Field:
    return Field(name, number(low), help, default=None, cli=decode_integer)
