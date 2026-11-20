"""Transactional intake, shipping, adjustment, and warehouse transfer."""

import sqlite3
from collections.abc import Iterable

from . import fields
from .catalog import Catalog
from .errors import Conflict, StockroomError
from .lot_rows import LotRows
from .models import Intake, Lot
from .movement_rows import MovementRows


def read_intake(row: Intake) -> Intake:
    return Intake(fields.code(row.sku, "sku"), fields.code(row.warehouse, "warehouse"),
                  fields.code(row.code, "lot"), fields.positive(row.quantity), fields.day(row.received),
                  fields.optional_code(row.supplier, "supplier"), fields.memo(row.memo))


class Inventory:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.catalog = Catalog(connection)
        self.rows = LotRows(connection)
        self.movements = MovementRows(connection)

    def receive(self, request: Intake) -> Lot:
        intake = read_intake(request)
        with self.connection:
            return self._receive(intake)

    def receive_all(self, requests: Iterable[Intake]) -> list[Lot]:
        incoming = [read_intake(request) for request in requests]
        with self.connection:
            return [self._receive(request) for request in incoming]

    def _receive(self, request: Intake) -> Lot:
        self.catalog.require_active(request.sku, request.warehouse, request.supplier)
        value = self._credit(request.sku, request.warehouse, request.code, request.quantity,
                             request.received, request.supplier)
        self.movements.add(value, request.received, "receive", request.quantity, request.memo)
        return value

    def _credit(self, sku: str, warehouse: str, code: str, quantity: int,
                received: str, supplier: str | None) -> Lot:
        original = self.rows.find(sku, warehouse, code)
        if original is None:
            return self.rows.create(sku, warehouse, code, quantity, received, supplier)
        if (original.received, original.supplier) != (received, supplier):
            raise Conflict("lot details differ")
        return self.rows.set_quantity(original.id, fields.positive(original.quantity + quantity))

    def _take(self, row: Lot, quantity: int, at: str, kind: str,
              note: str, counterpart: str | None = None) -> Lot:
        adjusted = self.rows.set_quantity(row.id, row.quantity - quantity)
        self.movements.add(adjusted, at, kind, -quantity, note, counterpart)
        return adjusted

    def _plan(self, sku: str, warehouse: str, quantity: int) -> list[tuple[Lot, int]]:
        remaining = quantity
        moves = []
        for lot in self.rows.list(sku, warehouse):
            piece = min(remaining, lot.quantity)
            moves.append((lot, piece))
            remaining -= piece
            if not remaining:
                return moves
        raise StockroomError("insufficient stock")

    def adjust(self, sku: str, warehouse: str, code: str, delta: int, at: str, note: str = "") -> Lot:
        sku = fields.code(sku, "sku")
        warehouse = fields.code(warehouse, "warehouse")
        code = fields.code(code, "lot")
        delta, at, note = fields.signed(delta), fields.day(at), fields.memo(note)
        with self.connection:
            lot = self.rows.require(sku, warehouse, code)
            quantity = lot.quantity + delta
            if quantity < 0:
                raise StockroomError("insufficient stock")
            if quantity > fields.SQLITE_MAX_INT:
                raise StockroomError("invalid quantity")
            updated = self.rows.set_quantity(lot.id, quantity)
            self.movements.add(updated, at, "adjust", delta, note)
        return updated

    def ship(self, sku: str, warehouse: str, quantity: int, at: str, note: str = "") -> list[Lot]:
        sku, warehouse = fields.code(sku, "sku"), fields.code(warehouse, "warehouse")
        quantity, at, note = fields.positive(quantity), fields.day(at), fields.memo(note)
        with self.connection:
            self.catalog.require_active(sku, warehouse)
            return [self._take(row, take, at, "ship", note) for row, take in self._plan(sku, warehouse, quantity)]

    def move(self, sku: str, source: str, destination: str, quantity: int, at: str,
             note: str = "") -> list[Lot]:
        sku = fields.code(sku, "sku")
        source, destination = fields.code(source, "warehouse"), fields.code(destination, "warehouse")
        quantity, at, note = fields.positive(quantity), fields.day(at), fields.memo(note)
        if source == destination:
            raise StockroomError("same warehouse")
        with self.connection:
            self.catalog.require_active(sku, source)
            self.catalog.require_active(sku, destination)
            output = []
            for row, take in self._plan(sku, source, quantity):
                self._take(row, take, at, "transfer", note, destination)
                added = self._credit(sku, destination, row.code, take, row.received, row.supplier)
                self.movements.add(added, at, "transfer", take, note, source)
                output.append(added)
            return output

    def lots(self, sku: str | None = None, warehouse: str | None = None,
             include_empty: bool = False) -> list[Lot]:
        return self.rows.list(fields.optional_code(sku, "sku"),
                              fields.optional_code(warehouse, "warehouse"), include_empty)
