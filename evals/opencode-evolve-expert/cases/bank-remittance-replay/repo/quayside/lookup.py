"""Search projected invoices by the reference printed on paperwork."""

from . import invoices, validation
from .errors import LedgerError
from .model import Invoice, State


def find(state: State, account: str, reference: str) -> Invoice:
    search = validation.label(reference, "reference").strip().lower()
    for row in invoices.listing(state, account):
        if row.reference.strip().lower() == search:
            return row
    raise LedgerError("unknown invoice reference")
