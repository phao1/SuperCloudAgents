"""Prepare an operator pick list without reserving or writing inventory."""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import fields
from .catalog import Catalog
from .errors import StockroomError
from .lot_rows import LotRows


@dataclass(frozen=True)
class Demand:
    sku: str
    quantity: int


@dataclass(frozen=True)
class Pick:
    sku: str
    warehouse: str
    lot: str
    lot_id: int
    quantity: int


@dataclass(frozen=True)
class Shortage:
    sku: str
    missing: int


@dataclass(frozen=True)
class PickList:
    picks: list[Pick]
    shortages: list[Shortage]


def load_requests(path: Path) -> list[Demand]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise StockroomError("invalid order file") from None
    if not isinstance(data, list):
        raise StockroomError("invalid order file")
    demands = []
    for row in data:
        if not isinstance(row, dict) or row.keys() != {"sku", "quantity"}:
            raise StockroomError("invalid order row")
        demands.append(Demand(row["sku"], row["quantity"]))
    return demands


class Fulfillment:
    def __init__(self, connection: sqlite3.Connection):
        self.catalog = Catalog(connection)
        self.lots = LotRows(connection)

    def _warehouses(self, selected: list[str] | None) -> list[str]:
        if selected is None:
            return [row.code for row in self.catalog.warehouses()]
        codes = [fields.code(code, "warehouse") for code in selected]
        if len(codes) != len(set(codes)):
            raise StockroomError("duplicate warehouse")
        for code in codes:
            if not self.catalog.rows.warehouse(code).active:
                raise StockroomError("inactive warehouse")
        return codes

    def _demand(self, orders: Iterable[Demand]) -> dict[str, int]:
        combined = {}
        for order in orders:
            item = self.catalog.item(order.sku)
            if not item.active:
                raise StockroomError("inactive item")
            amount = fields.positive(order.quantity)
            combined[item.sku] = fields.positive(combined.get(item.sku, 0) + amount)
        return combined

    def plan(self, orders: Iterable[Demand], warehouses: list[str] | None = None) -> PickList:
        sites = self._warehouses(warehouses)
        demand = self._demand(orders)
        picks, shortages = [], []
        for sku, available in demand.items():
            for warehouse in sites:
                for lot in self.lots.list(sku, warehouse):
                    amount = min(lot.quantity, available)
                    picks.append(Pick(sku, warehouse, lot.code, lot.id, amount))
                    available -= amount
                    if not available:
                        break
                if not available:
                    break
            if available:
                shortages.append(Shortage(sku, available))
        return PickList(picks, shortages)
