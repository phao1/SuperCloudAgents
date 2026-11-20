"""Append and query inventory movements."""

import sqlite3

from .models import Lot, Movement
from .query import Conditions

KINDS = ("receive", "adjust", "transfer", "ship")


class MovementRows:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def add(self, lot: Lot, day: str, kind: str, delta: int,
            memo: str = "", counterpart: str | None = None) -> None:
        self.connection.execute(
            """INSERT INTO movement(sku, warehouse, lot_id, day, kind, delta, counterpart, memo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (lot.sku, lot.warehouse, lot.id, day, kind, delta, counterpart, memo),
        )

    def list(self, sku: str | None = None, warehouse: str | None = None,
             since: str | None = None, through: str | None = None,
             kind: str | None = None, limit: int | None = None) -> list[Movement]:
        query = Conditions()
        query.equals("m.sku", sku)
        query.equals("m.warehouse", warehouse)
        query.equals("m.kind", kind)
        query.compare("m.day", ">=", since)
        query.compare("m.day", "<=", through)
        sql = """SELECT m.id, m.sku, m.warehouse, l.code AS lot, m.day, m.kind, m.delta, m.counterpart, m.memo
                 FROM movement AS m JOIN lot AS l ON m.lot_id = l.id"""
        sql += query.where() + " ORDER BY m.day, m.id"
        if limit is not None:
            sql += " LIMIT ?"
            query.arguments.append(limit)
        return [Movement(**dict(row)) for row in self.connection.execute(sql, query.arguments)]
