"""Resolve an as-of direct rate for a signed position."""

from dataclasses import replace
from decimal import Decimal, localcontext

from . import money, validation
from .errors import LedgerError
from .model import Rate, State


def put(state: State, source: str, target: str, day: str, rate: str) -> tuple[State, Rate]:
    source, target = money.currency(source), money.currency(target)
    day, value = validation.day(day), money.decimal(rate)
    if value <= 0 or (source == target and value != 1):
        raise LedgerError("invalid rate")
    replacement = Rate(source, target, day, format(value.normalize(), "f"))
    remaining = [row for row in state.rates if (row.source, row.target, row.day) != (source, target, day)]
    return replace(state, rates=tuple(sorted((*remaining, replacement),
                                            key=lambda row: (row.source, row.target, row.day)))), replacement


def effective(state: State, source: str, target: str, at: str) -> Decimal:
    source, target, at = money.currency(source), money.currency(target), validation.day(at)
    if source == target:
        return Decimal(1)
    candidates = [row for row in state.rates if row.source == source and row.target == target and row.day <= at]
    if not candidates:
        raise LedgerError(f"missing rate {source}/{target} for {at}")
    return Decimal(max(candidates, key=lambda row: row.day).value)


def convert(state: State, source: str, target: str, at: str, amount: Decimal) -> Decimal:
    rate = effective(state, source, target, at)
    with localcontext(prec=60):
        return money.round_to(amount * rate, target)


def quote(state: State, source: str, target: str, at: str, value: str) -> str:
    return money.format_fixed(convert(state, source, target, at, money.amount(value, source)), target)
