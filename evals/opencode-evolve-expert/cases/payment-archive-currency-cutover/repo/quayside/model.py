"""Portable records and the local ledger snapshot."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Account:
    code: str
    name: str
    currency: str


@dataclass(frozen=True)
class Line:
    description: str
    units: int
    unit_price: str


@dataclass(frozen=True)
class Invoice:
    id: int
    account: str
    reference: str
    day: str
    lines: tuple[Line, ...]


@dataclass(frozen=True)
class Payment:
    id: int
    account: str
    day: str
    amount: str
    reference: str


@dataclass(frozen=True)
class Rate:
    source: str
    target: str
    day: str
    value: str


@dataclass(frozen=True)
class State:
    accounts: dict[str, Account] = field(default_factory=dict)
    invoices: tuple[Invoice, ...] = ()
    payments: tuple[Payment, ...] = ()
    rates: tuple[Rate, ...] = ()


@dataclass(frozen=True)
class Entry:
    day: str
    kind: str
    id: int
    account: str
    reference: str
    currency: str
    amount: str


@dataclass(frozen=True)
class Statement:
    entries: list[Entry]
    totals: dict[str, str]
