"""Reconstruct lot balances for a historical day from the movement log."""

import sqlite3
from dataclasses import dataclass

from . import fields
from .query import Conditions


@dataclass(frozen=True)
class HistoricalLot:
    lot_id: int
    sku: str
    warehouse: str
    code: str
    received: str
    balance: int
    movements: int
    latest: str


class History:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def lots_at(self, day: str, sku: str | None = None, warehouse: str | None = None,
                include_zero: bool = False) -> list[HistoricalLot]:
        """Return lots with at least one movement through day, inclusive.

        A lot's history follows the recorded business day, which can differ
        from creation order. Historical balances may be negative when a late
        intake was recorded with a date after a correction. Keep those rows.
        """
        day = fields.day(day)
        sku = fields.optional_code(sku, "sku")
        warehouse = fields.optional_code(warehouse, "warehouse")
        query = Conditions()
        query.compare("m.day", "<=", day)
        query.equals("l.sku", sku)
        query.equals("l.warehouse", warehouse)
        sql = """SELECT l.id AS lot_id, l.sku, l.warehouse, l.code, l.received,
                        SUM(m.delta) AS balance, COUNT(m.id) AS movements, MAX(m.day) AS latest
                 FROM movement AS m JOIN lot AS l ON l.id = m.lot_id"""
        sql += query.where() + " GROUP BY l.id"
        if not include_zero:
            sql += " HAVING SUM(m.delta) <> 0"
        sql += " ORDER BY l.warehouse, l.sku, l.received, l.id"
        return [HistoricalLot(**dict(row)) for row in self.connection.execute(sql, query.arguments)]
