"""Portable catalog exchange and transactional receipt imports."""

import csv
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path

from .catalog import Catalog, make_item, make_supplier, make_warehouse
from .errors import StockroomError
from .inventory import Inventory
from .models import Intake, Lot
from .output import json_text

CATALOG_KEYS = ("warehouses", "suppliers", "items")
RECEIPT_KEYS = ("sku", "warehouse", "code", "quantity", "received", "supplier", "memo")
REQUIRED_RECEIPT = set(RECEIPT_KEYS[:5])


def read_catalog(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise StockroomError("invalid catalog file") from error
    if not isinstance(raw, dict) or not raw.keys() <= set(CATALOG_KEYS):
        raise StockroomError("invalid catalog fields")
    if any(not isinstance(group, list) or any(not isinstance(row, dict) for row in group)
           for group in raw.values()):
        raise StockroomError("invalid catalog groups")
    return raw


def import_catalog(connection: sqlite3.Connection, path: Path) -> dict[str, int]:
    data = read_catalog(path)
    try:
        warehouses = [make_warehouse(**entry) for entry in data.get("warehouses", [])]
        suppliers = [make_supplier(**entry) for entry in data.get("suppliers", [])]
        items = [make_item(**entry) for entry in data.get("items", [])]
    except TypeError:
        raise StockroomError("invalid catalog record") from None
    rows = Catalog(connection).rows
    with connection:
        for value in warehouses:
            rows.add_warehouse(value)
        for value in suppliers:
            rows.add_supplier(value)
        for value in items:
            rows.add_item(value)
    return {"warehouses": len(warehouses), "suppliers": len(suppliers), "items": len(items)}


def export_catalog(connection: sqlite3.Connection, include_inactive: bool = False) -> str:
    catalog = Catalog(connection)
    return json_text({
        "warehouses": [asdict(row) for row in catalog.warehouses(include_inactive)],
        "suppliers": [asdict(row) for row in catalog.suppliers(include_inactive)],
        "items": [asdict(row) for row in catalog.items(include_inactive=include_inactive)],
    })


def csv_requests(lines: Iterable[str]) -> list[Intake]:
    reader = csv.DictReader(lines, strict=True)
    columns = reader.fieldnames
    if (columns is None or len(set(columns)) != len(columns) or not REQUIRED_RECEIPT <= set(columns)
            or not set(columns) <= set(RECEIPT_KEYS)):
        raise StockroomError("invalid receipt columns")
    out = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise StockroomError(f"invalid receipt line {reader.line_num}")
        try:
            quantity = int(row["quantity"])
        except ValueError:
            raise StockroomError(f"invalid receipt line {reader.line_num}") from None
        out.append(Intake(row["sku"], row["warehouse"], row["code"], quantity, row["received"],
                          row.get("supplier") or None, row.get("memo") or ""))
    return out


def import_receipts(connection: sqlite3.Connection, path: Path) -> list[Lot]:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            requests = csv_requests(stream)
    except (OSError, UnicodeError, csv.Error):
        raise StockroomError("invalid receipt file") from None
    return Inventory(connection).receive_all(requests)
