"""Physical inventory counts with review before adjusting stock."""

import sqlite3

from . import fields
from .catalog_rows import CatalogRows
from .errors import Conflict, StockroomError
from .lot_rows import LotRows
from .models import Observation, Stocktake
from .movement_rows import MovementRows
from .stocktake_rows import CountRows

STATES = ("open", "closed", "aborted")


class Counts:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.catalog = CatalogRows(connection)
        self.lots = LotRows(connection)
        self.movements = MovementRows(connection)
        self.rows = CountRows(connection)

    def open(self, warehouse: str, label: str, at: str) -> Stocktake:
        warehouse = fields.code(warehouse, "warehouse")
        label, at = fields.title(label, "label"), fields.day(at)
        if not self.catalog.warehouse(warehouse).active:
            raise StockroomError("inactive warehouse")
        with self.connection:
            return self.rows.create(warehouse, label, at)

    def _open_session(self, stocktake_id: int) -> Stocktake:
        value = self.rows.get(fields.positive(stocktake_id))
        if value.status != "open":
            raise Conflict("stocktake is closed")
        return value

    def count(self, stocktake_id: int, lot_id: int, quantity: int | None) -> Observation:
        lot_id = fields.positive(lot_id)
        if quantity is not None and (type(quantity) is not int or not 0 <= quantity <= fields.SQLITE_MAX_INT):
            raise StockroomError("invalid quantity")
        with self.connection:
            session = self._open_session(stocktake_id)
            return self.rows.observe(session.id, lot_id, quantity)

    def review(self, stocktake_id: int, differences_only: bool = False) -> list[Observation]:
        session = self.rows.get(fields.positive(stocktake_id))
        observations = self.rows.observations(session.id)
        return [row for row in observations if not differences_only or row.difference]

    def list(self, warehouse: str | None = None, status: str | None = None) -> list[Stocktake]:
        warehouse = fields.optional_code(warehouse, "warehouse")
        if status is not None and status not in STATES:
            raise StockroomError("invalid stocktake status")
        return self.rows.sessions(warehouse, status)

    def _finish(self, stocktake_id: int, at: str, *, adjust: bool) -> Stocktake:
        at = fields.day(at)
        with self.connection:
            session = self._open_session(stocktake_id)
            if at < session.opened:
                raise StockroomError("close before open")
            if adjust:
                records = self.rows.observations(session.id)
                if any(row.observed is None for row in records):
                    raise Conflict("count sheet is incomplete")
                for row in records:
                    lot = self.lots.by_id(row.lot_id)
                    if lot.quantity != row.expected:
                        raise Conflict("stock changed during count")
                    if row.difference:
                        adjusted = self.lots.set_quantity(row.lot_id, row.observed)
                        self.movements.add(adjusted, at, "adjust", row.difference, f"stocktake {session.id}")
            return self.rows.finish(session.id, "closed" if adjust else "aborted", at)

    def close(self, stocktake_id: int, at: str) -> Stocktake:
        return self._finish(stocktake_id, at, adjust=True)

    def abort(self, stocktake_id: int, at: str) -> Stocktake:
        return self._finish(stocktake_id, at, adjust=False)
