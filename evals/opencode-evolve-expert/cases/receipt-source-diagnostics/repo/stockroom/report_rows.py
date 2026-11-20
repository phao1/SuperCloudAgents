"""Inventory reporting prepares independent, ordered projections."""

import sqlite3

from .models import AuditLine, Balance
from .query import Conditions


class ReportRows:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def balances(self, sku: str | None, warehouse: str | None) -> list[Balance]:
        query = Conditions()
        query.equals("sku", sku)
        query.equals("warehouse", warehouse)
        query.compare("quantity", ">", 0)
        sql = ("SELECT sku, warehouse, SUM(quantity) AS quantity, COUNT(*) AS positive_lots FROM lot"
               + query.where() + " GROUP BY warehouse, sku ORDER BY warehouse, sku")
        return [Balance(**dict(row)) for row in self.connection.execute(sql, query.arguments)]

    def audit(self, sku: str | None, warehouse: str | None) -> list[AuditLine]:
        query = Conditions()
        query.equals("l.sku", sku)
        query.equals("l.warehouse", warehouse)
        sql = """SELECT l.id AS lot_id, l.sku, l.warehouse, l.code, l.quantity,
                        COALESCE(SUM(m.delta), 0) AS recorded, l.quantity - COALESCE(SUM(m.delta), 0) AS difference
                 FROM lot AS l LEFT JOIN movement AS m ON l.id = m.lot_id"""
        sql += query.where() + " GROUP BY l.id ORDER BY l.warehouse, l.sku, l.received, l.id"
        return [AuditLine(**dict(row)) for row in self.connection.execute(sql, query.arguments)]
