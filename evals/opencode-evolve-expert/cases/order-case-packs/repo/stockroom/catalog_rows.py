"""SQL operations for the catalog. The service owns transaction scope."""

import sqlite3

from .errors import Conflict, Missing
from .models import Item, Supplier, Warehouse
from .query import Conditions


class CatalogRows:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def warehouse(self, code: str) -> Warehouse:
        row = self.connection.execute(
            "SELECT code, name, active FROM warehouse WHERE code = ?", (code,),
        ).fetchone()
        if row is None:
            raise Missing("unknown warehouse")
        return Warehouse(row["code"], row["name"], bool(row["active"]))

    def supplier(self, code: str) -> Supplier:
        row = self.connection.execute(
            "SELECT code, name, contact, active FROM supplier WHERE code = ?", (code,),
        ).fetchone()
        if row is None:
            raise Missing("unknown supplier")
        return Supplier(row["code"], row["name"], row["contact"], bool(row["active"]))

    def item(self, sku: str) -> Item:
        row = self.connection.execute(
            "SELECT sku, name, category, unit, active FROM item WHERE sku = ?", (sku,),
        ).fetchone()
        if row is None:
            raise Missing("unknown sku")
        return self._item(row)

    @staticmethod
    def _item(row: sqlite3.Row) -> Item:
        return Item(row["sku"], row["name"], row["category"], row["unit"], bool(row["active"]))

    def add_warehouse(self, value: Warehouse) -> None:
        try:
            self.connection.execute(
                "INSERT INTO warehouse(code, name, active) VALUES (?, ?, ?)",
                (value.code, value.name, value.active),
            )
        except sqlite3.IntegrityError:
            raise Conflict("warehouse already exists") from None

    def add_supplier(self, value: Supplier) -> None:
        try:
            self.connection.execute(
                "INSERT INTO supplier(code, name, contact, active) VALUES (?, ?, ?, ?)",
                (value.code, value.name, value.contact, value.active),
            )
        except sqlite3.IntegrityError:
            raise Conflict("supplier already exists") from None

    def add_item(self, value: Item) -> None:
        try:
            self.connection.execute(
                "INSERT INTO item(sku, name, category, unit, active) VALUES (?, ?, ?, ?, ?)",
                (value.sku, value.name, value.category, value.unit, value.active),
            )
        except sqlite3.IntegrityError:
            raise Conflict("sku already exists") from None

    def warehouses(self, include_inactive: bool = False) -> list[Warehouse]:
        query = Conditions()
        query.equals("active", None if include_inactive else 1)
        rows = self.connection.execute(
            "SELECT code, name, active FROM warehouse" + query.where() + " ORDER BY code",
            query.arguments,
        )
        return [Warehouse(row["code"], row["name"], bool(row["active"])) for row in rows]

    def suppliers(self, include_inactive: bool = False) -> list[Supplier]:
        query = Conditions()
        query.equals("active", None if include_inactive else 1)
        rows = self.connection.execute(
            "SELECT code, name, contact, active FROM supplier" + query.where() + " ORDER BY code",
            query.arguments,
        )
        return [Supplier(row["code"], row["name"], row["contact"], bool(row["active"])) for row in rows]

    def items(self, category: str | None = None, include_inactive: bool = False) -> list[Item]:
        query = Conditions()
        query.equals("active", None if include_inactive else 1)
        query.equals("category", category)
        rows = self.connection.execute(
            "SELECT sku, name, category, unit, active FROM item" + query.where() + " ORDER BY sku",
            query.arguments,
        )
        return [self._item(row) for row in rows]

    def set_warehouse_active(self, code: str, active: bool) -> None:
        result = self.connection.execute("UPDATE warehouse SET active = ? WHERE code = ?", (active, code))
        if not result.rowcount:
            raise Missing("unknown warehouse")

    def set_supplier_active(self, code: str, active: bool) -> None:
        result = self.connection.execute("UPDATE supplier SET active = ? WHERE code = ?", (active, code))
        if not result.rowcount:
            raise Missing("unknown supplier")

    def set_item_active(self, sku: str, active: bool) -> None:
        result = self.connection.execute("UPDATE item SET active = ? WHERE sku = ?", (active, sku))
        if not result.rowcount:
            raise Missing("unknown sku")
