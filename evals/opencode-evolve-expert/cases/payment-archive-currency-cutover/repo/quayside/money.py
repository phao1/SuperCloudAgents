"""Currency scale and decimal wire forms shared in the ledger."""

import re
from decimal import ROUND_HALF_UP, Decimal, localcontext

from .errors import LedgerError

PLACES = {"USD": 2, "EUR": 2, "CHF": 2, "JPY": 0, "KWD": 3}
DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]{0,13})(?:\.[0-9]{1,9})?")


def currency(value: str) -> str:
    if not isinstance(value, str) or value.upper() not in PLACES or not value.isascii():
        raise LedgerError("unknown currency")
    return value.upper()


def decimal(value: str) -> Decimal:
    if not isinstance(value, str) or not DECIMAL.fullmatch(value):
        raise LedgerError("invalid decimal amount")
    return Decimal(value)


def quantum(code: str) -> Decimal:
    return Decimal(1).scaleb(-PLACES[currency(code)])


def amount(value: str, code: str) -> Decimal:
    number = decimal(value)
    scale = quantum(code)
    with localcontext(prec=60):
        if number != number.quantize(scale):
            raise LedgerError("amount exceeds currency precision")
        return number.quantize(scale)


def round_to(number: Decimal, code: str, mode: str = ROUND_HALF_UP) -> Decimal:
    with localcontext(prec=60):
        return number.quantize(quantum(code), rounding=mode)


def format_fixed(number: Decimal, code: str) -> str:
    quantized = round_to(number, code)
    if quantized.is_zero():
        quantized = abs(quantized)
    return format(quantized, "f")
