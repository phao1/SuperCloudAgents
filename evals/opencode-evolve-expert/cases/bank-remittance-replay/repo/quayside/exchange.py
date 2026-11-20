"""Portable payment archive plus reporting wire projections."""

from decimal import Decimal
from pathlib import Path

from . import payments, storage
from .errors import LedgerError
from .model import Payment, State

V1 = frozenset(("account", "day", "reference", "cents"))


def dump(state: State, path: Path, account: str | None = None) -> int:
    items = []
    for row in payments.listing(state, account):
        items.append({"account": row.account, "day": row.day, "reference": row.reference,
                      "cents": int(Decimal(row.amount) * 100)})
    storage.write(path, {"version": 1, "payments": items})
    return len(items)


def read_archive(path: Path) -> list[dict[str, str]]:
    data = storage.read(path)
    if (not isinstance(data, dict) or data.keys() != {"version", "payments"}
            or type(data["version"]) is not int or data["version"] != 1
            or not isinstance(data["payments"], list)):
        raise LedgerError("invalid archive version or shape")
    result = []
    for row in data["payments"]:
        if not isinstance(row, dict) or row.keys() != V1 or type(row["cents"]) is not int:
            raise LedgerError("invalid archive payment")
        result.append({
            "account": row["account"], "day": row["day"], "reference": row["reference"],
            "amount": format(Decimal(row["cents"]) / 100, ".2f"),
        })
    return result


def restore(state: State, path: Path) -> tuple[State, list[Payment]]:
    return payments.add_many(state, read_archive(path))
