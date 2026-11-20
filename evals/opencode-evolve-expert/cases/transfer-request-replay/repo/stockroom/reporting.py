"""Current balances and historical activity queries."""

import sqlite3
from collections import defaultdict
from datetime import date

from . import fields
from .errors import StockroomError
from .lot_rows import LotRows
from .models import Aging, AuditLine, Balance, Lot, Movement, Trend
from .movement_rows import KINDS, MovementRows
from .report_rows import ReportRows
from .trend import rollup


class Reporting:
    def __init__(self, connection: sqlite3.Connection):
        self.rows = ReportRows(connection)
        self.lots = LotRows(connection)
        self.movements = MovementRows(connection)

    def balances(self, sku: str | None = None, warehouse: str | None = None) -> list[Balance]:
        return self.rows.balances(fields.optional_code(sku, "sku"),
                                  fields.optional_code(warehouse, "warehouse"))

    def activity(self, sku: str | None = None, warehouse: str | None = None,
                 since: str | None = None, through: str | None = None,
                 kind: str | None = None, limit: int | None = None) -> list[Movement]:
        sku = fields.optional_code(sku, "sku")
        warehouse = fields.optional_code(warehouse, "warehouse")
        since, through = fields.order_days(since, through)
        if kind is not None and kind not in KINDS:
            raise StockroomError("invalid movement kind")
        if limit is not None:
            limit = fields.positive(limit)
        return self.movements.list(sku, warehouse, since, through, kind, limit)

    def aging(self, at: str, sku: str | None = None, warehouse: str | None = None) -> list[Aging]:
        today = date.fromisoformat(fields.day(at))
        sku, warehouse = fields.optional_code(sku, "sku"), fields.optional_code(warehouse, "warehouse")
        groups: dict[tuple[str, str], list[Lot]] = defaultdict(list)
        for row in self.lots.list(sku, warehouse):
            if row.received <= at:
                groups[row.warehouse, row.sku].append(row)
        result = []
        for (warehouse, sku), lots in groups.items():
            ages = [(today - date.fromisoformat(row.received)).days for row in lots]
            result.append(Aging(sku, warehouse, sum(row.quantity for row in lots), min(ages), max(ages)))
        return result

    def audit(self, sku: str | None = None, warehouse: str | None = None,
              mismatches_only: bool = False) -> list[AuditLine]:
        sku = fields.optional_code(sku, "sku")
        warehouse = fields.optional_code(warehouse, "warehouse")
        lines = self.rows.audit(sku, warehouse)
        return [line for line in lines if not mismatches_only or line.difference]


    def trend(self, period: str, sku: str | None = None, warehouse: str | None = None,
              since: str | None = None, through: str | None = None) -> list[Trend]:
        return rollup(self.activity(sku, warehouse, since, through), period)
