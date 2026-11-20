"""Allocate a desk's native deposits over outstanding invoices."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from . import accounts, invoices, money, validation
from .model import State


@dataclass(frozen=True)
class Outstanding:
    account: str
    invoice: int
    reference: str
    day: str
    age_days: int
    currency: str
    remaining: str


@dataclass(frozen=True)
class Aging:
    rows: list[Outstanding]
    unallocated: dict[str, str]


def report(state: State, through: str, account: str | None = None) -> Aging:
    through = validation.day(through)
    selected = accounts.listing(state) if account is None else [accounts.get(state, account)]
    scope = {site.code: site.currency for site in selected}
    cash: dict[str, Decimal] = defaultdict(Decimal)
    rows = []
    with localcontext(prec=60):
        for deposit in state.payments:
            if deposit.account in scope and deposit.day <= through:
                cash[deposit.account] += Decimal(deposit.amount)
        ordered = sorted(state.invoices, key=lambda invoice: (invoice.account, invoice.day, invoice.id))
        for invoice in ordered:
            if invoice.account not in scope or invoice.day > through:
                continue
            total = invoices.total(invoice)
            sent = min(total, max(cash[invoice.account], Decimal(0)))
            cash[invoice.account] -= sent
            remaining = total - sent
            if remaining:
                code = scope[invoice.account]
                age = (date.fromisoformat(through) - date.fromisoformat(invoice.day)).days
                rows.append(Outstanding(invoice.account, invoice.id, invoice.reference, invoice.day,
                                        age, code, money.format_fixed(remaining, code)))
        return Aging(rows, {account: money.format_fixed(cash[account], code)
                            for account, code in scope.items()})
