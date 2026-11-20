"""Atomic disk snapshots and common JSON document validation."""

import json
import os
from dataclasses import asdict
from pathlib import Path
from tempfile import NamedTemporaryFile

from .errors import LedgerError
from .model import Account, Invoice, Line, Payment, Rate, State


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise LedgerError("duplicate JSON field")
        value[key] = item
    return value


def _nonfinite(value):
    raise LedgerError("non-finite JSON constant")


def read(path: Path, *, missing=None):
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return missing
    except (OSError, UnicodeError):
        raise LedgerError(f"cannot read {path.name}") from None
    try:
        return json.loads(raw, object_pairs_hook=_unique, parse_constant=_nonfinite)
    except (ValueError, RecursionError):
        raise LedgerError(f"invalid JSON in {path.name}") from None


def text(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=asdict) + "\n"


def write(path: Path, value) -> None:
    replace_text(path, text(value))


def replace_text(path: Path, content: str) -> None:
    scratch = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False,
                                prefix=f".{path.name}-") as output:
            scratch = Path(output.name)
            output.write(content)
        os.replace(scratch, path)
        scratch = None
    except OSError:
        raise LedgerError(f"cannot write {path.name}") from None
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)


def _invoice(row: dict) -> Invoice:
    return Invoice(row["id"], row["account"], row["reference"], row["day"],
                   tuple(Line(**line) for line in row["lines"]))


def load(path: Path) -> State:
    data = read(path, missing={})
    try:
        return State(
            accounts={code: Account(**row) for code, row in data.get("accounts", {}).items()},
            invoices=tuple(_invoice(row) for row in data.get("invoices", [])),
            payments=tuple(Payment(**row) for row in data.get("payments", [])),
            rates=tuple(Rate(**row) for row in data.get("rates", [])),
        )
    except (AttributeError, TypeError, KeyError):
        raise LedgerError("invalid ledger state") from None
