"""Project native postings or convert them to the report currency."""

from collections import defaultdict
from decimal import Decimal, localcontext

from . import accounts, invoices, money, rates, validation
from .model import Entry, State, Statement


def project(state: State, first: str, last: str, currency: str | None = None,
            account: str | None = None) -> Statement:
    first, last = validation.window(first, last)
    target = None if currency is None else money.currency(currency)
    scope = None if account is None else accounts.get(state, account).code
    entries = []
    with localcontext(prec=60):
        for row in state.invoices:
            if first <= row.day <= last and (scope is None or row.account == scope):
                source = state.accounts[row.account].currency
                entries.append(entry(state, row.day, "invoice", row.id, row.account,
                                     row.reference, source, target, invoices.total(row)))
        for row in state.payments:
            if first <= row.day <= last and (scope is None or row.account == scope):
                source = state.accounts[row.account].currency
                entries.append(entry(state, row.day, "payment", row.id, row.account,
                                     row.reference, source, target, -Decimal(row.amount)))
        entries.sort(key=lambda row: (row.day, row.kind, row.id))
        totals = defaultdict(Decimal)
        for row in entries:
            totals[row.currency] += Decimal(row.amount)
        return Statement(entries, {code: money.format_fixed(totals[code], code) for code in sorted(totals)})


def entry(state: State, day: str, kind: str, identity: int, account: str,
          reference: str, source: str, target: str | None, amount: Decimal) -> Entry:
    if target is not None:
        amount = rates.convert(state, source, target, day, amount)
    code = source if target is None else target
    return Entry(day, kind, identity, account, reference, code, money.format_fixed(amount, code))
