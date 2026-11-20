"""Prepare the cashier's date-ordered native payment register."""

from collections.abc import Iterable
from dataclasses import astuple, dataclass, fields
from pathlib import Path

from . import payments, storage, validation
from .model import State
from .output import csv_text


@dataclass(frozen=True)
class RegisterRow:
    day: str
    id: int
    account: str
    currency: str
    reference: str
    amount: str


def rows(state: State, account: str | None = None, since: str | None = None,
         through: str | None = None) -> list[RegisterRow]:
    start = None if since is None else validation.day(since)
    end = None if through is None else validation.day(through)
    if start is not None and end is not None:
        validation.window(start, end)
    return [
        RegisterRow(row.day, row.id, row.account, state.accounts[row.account].currency,
                    row.reference, row.amount)
        for row in sorted(payments.listing(state, account), key=lambda row: (row.day, row.id))
        if (start is None or row.day >= start) and (end is None or row.day <= end)
    ]


def serialize(entries: Iterable[RegisterRow]) -> str:
    return csv_text([field.name for field in fields(RegisterRow)], map(astuple, entries))


def dump(state: State, path: Path, account: str | None = None,
         since: str | None = None, through: str | None = None) -> int:
    entries = rows(state, account, since, through)
    storage.replace_text(path, serialize(entries))
    return len(entries)
