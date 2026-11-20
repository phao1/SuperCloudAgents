"""Post bank activity without changing issued invoices."""

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from . import accounts, money, storage, validation
from .errors import LedgerError
from .model import Payment, State


def prepare(state: State, identity: int, account: str, day: str,
            amount: str, reference: str) -> Payment:
    site = accounts.get(state, account)
    day = validation.day(day)
    value = money.amount(amount, site.currency)
    if value.is_zero():
        raise LedgerError("payment cannot be zero")
    return Payment(identity, site.code, day, money.format_fixed(value, site.currency),
                   validation.label(reference, "reference"))


def add_many(state: State, rows: Iterable[dict[str, str]]) -> tuple[State, list[Payment]]:
    start = max((row.id for row in state.payments), default=0)
    prepared = [
        prepare(state, identity, **raw) for identity, raw in enumerate(rows, start + 1)
    ]
    return replace(state, payments=(*state.payments, *prepared)), prepared


def add(state: State, account: str, day: str, amount: str,
        reference: str) -> tuple[State, Payment]:
    updated, rows = add_many(state, [{"account": account, "day": day, "amount": amount, "reference": reference}])
    return updated, rows[0]


def read_bank(path: Path) -> list[dict[str, str]]:
    data = storage.read(path)
    if not isinstance(data, list) or any(
        not isinstance(row, dict) or row.keys() != {"account", "day", "amount", "reference"}
        for row in data
    ):
        raise LedgerError("invalid remittance rows")
    return data


def import_bank(state: State, path: Path) -> tuple[State, list[Payment]]:
    return add_many(state, read_bank(path))


def listing(state: State, account: str | None = None) -> list[Payment]:
    code = None if account is None else accounts.get(state, account).code
    return [row for row in state.payments if code is None or row.account == code]
