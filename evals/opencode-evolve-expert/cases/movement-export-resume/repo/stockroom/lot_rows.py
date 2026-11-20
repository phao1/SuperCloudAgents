"""Lot reads and writes with a stable FIFO order."""

import sqlite3

from .errors import Missing
from .models import Lot
from .query import Conditions

SELECT_LOT = "SELECT id, sku, warehouse, code, quantity, received, supplier FROM lot"


class LotRows:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def find(self, sku: str, warehouse: str, code: str) -> Lot | None:
        row = self.connection.execute(
            SELECT_LOT + " WHERE sku = ? AND warehouse = ? AND code = ?", (sku, warehouse, code),
        ).fetchone()
        return None if row is None else Lot(**dict(row))

    def require(self, sku: str, warehouse: str, code: str) -> Lot:
        value = self.find(sku, warehouse, code)
        if value is None:
            raise Missing("unknown lot")
        return value

    def by_id(self, lot_id: int) -> Lot:
        row = self.connection.execute(SELECT_LOT + " WHERE id = ?", (lot_id,)).fetchone()
        if row is None:
            raise Missing("unknown lot")
        return Lot(**dict(row))

    def create(self, sku: str, warehouse: str, code: str, quantity: int,
               received: str, supplier: str | None) -> Lot:
        result = self.connection.execute(
            "INSERT INTO lot(sku, warehouse, code, quantity, received, supplier) VALUES (?, ?, ?, ?, ?, ?)",
            (sku, warehouse, code, quantity, received, supplier),
        )
        return self.by_id(result.lastrowid)

    def set_quantity(self, lot_id: int, quantity: int) -> Lot:
        self.connection.execute("UPDATE lot SET quantity = ? WHERE id = ?", (quantity, lot_id))
        return self.by_id(lot_id)

    def list(self, sku: str | None = None, warehouse: str | None = None,
             include_empty: bool = False) -> list[Lot]:
        query = Conditions()
        query.equals("sku", sku)
        query.equals("warehouse", warehouse)
        if not include_empty:
            query.compare("quantity", ">", 0)
        rows = self.connection.execute(
            SELECT_LOT + query.where() + " ORDER BY warehouse, sku, received, id", query.arguments,
        )
        return [Lot(**dict(row)) for row in rows]
