"""Issue native-currency invoices, preserving source line order."""

from collections.abc import Iterable
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path

from . import accounts, money, storage, validation
from .errors import LedgerError
from .model import Invoice, Line, State


def read_lines(path: Path) -> list[Line]:
    data = storage.read(path)
    if not isinstance(data, list):
        raise LedgerError("invalid invoice file")
    if any(not isinstance(row, dict) or row.keys() != {"description", "units", "unit_price"}
           for row in data):
        raise LedgerError("invalid invoice line")
    return [Line(**row) for row in data]


def issue(state: State, account: str, reference: str,
          day: str, lines: Iterable[Line]) -> tuple[State, Invoice]:
    site = accounts.get(state, account)
    label = validation.label(reference, "reference")
    day = validation.day(day)
    if any(row.account == site.code and row.reference.strip().lower() == label.strip().lower()
           for row in state.invoices):
        raise LedgerError("invoice reference already exists")
    result = []
    for row in lines:
        description = validation.label(row.description, "description")
        units = validation.integer(row.units, "units")
        unit_price = money.amount(row.unit_price, site.currency)
        if unit_price < 0:
            raise LedgerError("negative unit price")
        result.append(Line(description, units, money.format_fixed(unit_price, site.currency)))
    if not result:
        raise LedgerError("invoice needs a line")
    identity = max((row.id for row in state.invoices), default=0) + 1
    invoice = Invoice(identity, site.code, label, day, tuple(result))
    return replace(state, invoices=(*state.invoices, invoice)), invoice


def get(state: State, identity: int) -> Invoice:
    validation.integer(identity, "invoice id")
    for row in state.invoices:
        if row.id == identity:
            return row
    raise LedgerError("unknown invoice")


def listing(state: State, account: str | None = None) -> list[Invoice]:
    scope = None if account is None else accounts.get(state, account).code
    return [row for row in state.invoices if scope is None or row.account == scope]


def line_value(row: Line) -> Decimal:
    with localcontext(prec=60):
        return Decimal(row.unit_price) * row.units


def total(record: Invoice) -> Decimal:
    with localcontext(prec=60):
        return sum((line_value(row) for row in record.lines), Decimal(0))
