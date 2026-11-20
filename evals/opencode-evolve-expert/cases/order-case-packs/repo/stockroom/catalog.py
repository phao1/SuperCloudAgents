"""Catalog operations used by interactive and scripted clients."""

import sqlite3

from . import fields
from .catalog_rows import CatalogRows
from .errors import StockroomError
from .models import Item, Supplier, Warehouse


def make_item(sku: str, name: str, *, category: str = "GENERAL", unit: str = "each") -> Item:
    return Item(fields.code(sku, "sku"), fields.title(name, "name"),
                fields.code(category, "category"), fields.title(unit, "unit"))


def make_warehouse(code: str, name: str) -> Warehouse:
    return Warehouse(fields.code(code, "warehouse"), fields.title(name, "name"))


def make_supplier(code: str, name: str, contact: str = "") -> Supplier:
    return Supplier(fields.code(code, "supplier"), fields.title(name, "name"), fields.memo(contact))


class Catalog:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.rows = CatalogRows(connection)

    def add_item(self, sku: str, name: str, *, category: str = "GENERAL", unit: str = "each") -> Item:
        model = make_item(sku, name, category=category, unit=unit)
        with self.connection:
            self.rows.add_item(model)
        return model

    def add_warehouse(self, code: str, name: str) -> Warehouse:
        model = make_warehouse(code, name)
        with self.connection:
            self.rows.add_warehouse(model)
        return model

    def add_supplier(self, code: str, name: str, contact: str = "") -> Supplier:
        model = make_supplier(code, name, contact)
        with self.connection:
            self.rows.add_supplier(model)
        return model

    def item(self, sku: str) -> Item:
        return self.rows.item(fields.code(sku, "sku"))

    def items(self, category: str | None = None, include_inactive: bool = False) -> list[Item]:
        return self.rows.items(fields.optional_code(category, "category"), include_inactive)

    def warehouses(self, include_inactive: bool = False) -> list[Warehouse]:
        return self.rows.warehouses(include_inactive)

    def suppliers(self, include_inactive: bool = False) -> list[Supplier]:
        return self.rows.suppliers(include_inactive)

    def item_status(self, sku: str, enabled: bool) -> Item:
        sku, enabled = fields.code(sku, "sku"), fields.active(enabled)
        with self.connection:
            self.rows.set_item_active(sku, enabled)
        return self.rows.item(sku)

    def warehouse_status(self, code: str, enabled: bool) -> Warehouse:
        code, enabled = fields.code(code, "warehouse"), fields.active(enabled)
        with self.connection:
            self.rows.set_warehouse_active(code, enabled)
        return self.rows.warehouse(code)

    def supplier_status(self, code: str, enabled: bool) -> Supplier:
        code, enabled = fields.code(code, "supplier"), fields.active(enabled)
        with self.connection:
            self.rows.set_supplier_active(code, enabled)
        return self.rows.supplier(code)

    def require_active(self, sku: str, warehouse: str, supplier: str | None = None) -> None:
        if not self.rows.item(sku).active:
            raise StockroomError("inactive item")
        if not self.rows.warehouse(warehouse).active:
            raise StockroomError("inactive warehouse")
        if supplier is not None and not self.rows.supplier(supplier).active:
            raise StockroomError("inactive supplier")
