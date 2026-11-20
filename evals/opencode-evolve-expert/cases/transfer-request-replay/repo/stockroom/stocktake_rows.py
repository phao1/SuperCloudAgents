"""Storage for stocktake sessions and dated count sheets."""

import sqlite3

from .errors import Missing
from .models import Observation, Stocktake
from .query import Conditions

TAKE = "SELECT id, warehouse, label, opened, closed, status FROM stocktake"


class CountRows:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, warehouse: str, label: str, at: str) -> Stocktake:
        row = self.connection.execute(
            "INSERT INTO stocktake(warehouse, label, opened) VALUES (?, ?, ?)", (warehouse, label, at),
        )
        self.connection.execute(
            """INSERT INTO observation(stocktake_id, lot_id, expected)
               SELECT ?, id, quantity FROM lot WHERE warehouse = ? AND quantity > 0""",
            (row.lastrowid, warehouse),
        )
        return self.get(row.lastrowid)

    def get(self, stocktake_id: int) -> Stocktake:
        row = self.connection.execute(TAKE + " WHERE id = ?", (stocktake_id,)).fetchone()
        if row is None:
            raise Missing("unknown stocktake")
        return Stocktake(**dict(row))

    def sessions(self, warehouse: str | None, status: str | None) -> list[Stocktake]:
        predicate = Conditions()
        predicate.equals("warehouse", warehouse)
        predicate.equals("status", status)
        rows = self.connection.execute(TAKE + predicate.where() + " ORDER BY id", predicate.arguments)
        return [Stocktake(**dict(row)) for row in rows]

    def observations(self, stocktake_id: int) -> list[Observation]:
        result = self.connection.execute(
            """SELECT o.lot_id, l.sku, l.code AS lot, o.expected, o.observed
               FROM observation AS o JOIN lot AS l ON o.lot_id = l.id
               WHERE o.stocktake_id = ? ORDER BY l.sku, l.received, l.id""",
            (stocktake_id,),
        )
        return [Observation(**dict(row)) for row in result]

    def observe(self, stocktake_id: int, lot_id: int, quantity: int | None) -> Observation:
        result = self.connection.execute(
            "UPDATE observation SET observed = ? WHERE stocktake_id = ? AND lot_id = ?",
            (quantity, stocktake_id, lot_id),
        )
        if not result.rowcount:
            raise Missing("lot outside stocktake")
        return next(row for row in self.observations(stocktake_id) if row.lot_id == lot_id)

    def finish(self, stocktake_id: int, status: str, at: str) -> Stocktake:
        self.connection.execute("UPDATE stocktake SET status = ?, closed = ? WHERE id = ?",
                                (status, at, stocktake_id))
        return self.get(stocktake_id)
