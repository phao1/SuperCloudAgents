"""Item, warehouse, and supplier command groups."""

from .catalog import Catalog
from .output import object_json, rows_json


def register(commands) -> None:
    items = commands.add_parser("item", help="manage item metadata")
    item = items.add_subparsers(dest="action", required=True)
    add = item.add_parser("add")
    add.add_argument("sku")
    add.add_argument("name")
    add.add_argument("--category", default="GENERAL")
    add.add_argument("--unit", default="each")
    add.set_defaults(action_fn=add_item)
    show = item.add_parser("show")
    show.add_argument("sku")
    show.set_defaults(action_fn=show_item)
    ls = item.add_parser("list")
    ls.add_argument("--category")
    ls.add_argument("--all", action="store_true")
    ls.set_defaults(action_fn=list_items)
    status = item.add_parser("status")
    status.add_argument("sku")
    status.add_argument("status", choices=("active", "inactive"))
    status.set_defaults(action_fn=item_status)

    warehouses = commands.add_parser("warehouse", help="manage warehouse metadata")
    warehouse = warehouses.add_subparsers(dest="action", required=True)
    add = warehouse.add_parser("add")
    add.add_argument("code")
    add.add_argument("name")
    add.set_defaults(action_fn=add_warehouse)
    ls = warehouse.add_parser("list")
    ls.add_argument("--all", action="store_true")
    ls.set_defaults(action_fn=list_warehouses)
    status = warehouse.add_parser("status")
    status.add_argument("code")
    status.add_argument("status", choices=("active", "inactive"))
    status.set_defaults(action_fn=warehouse_status)

    suppliers = commands.add_parser("supplier", help="manage supplier metadata")
    supplier = suppliers.add_subparsers(dest="action", required=True)
    add = supplier.add_parser("add")
    add.add_argument("code")
    add.add_argument("name")
    add.add_argument("--contact", default="")
    add.set_defaults(action_fn=add_supplier)
    ls = supplier.add_parser("list")
    ls.add_argument("--all", action="store_true")
    ls.set_defaults(action_fn=list_suppliers)
    status = supplier.add_parser("status")
    status.add_argument("code")
    status.add_argument("status", choices=("active", "inactive"))
    status.set_defaults(action_fn=supplier_status)


def add_item(connection, args) -> str:
    return object_json(Catalog(connection).add_item(args.sku, args.name, category=args.category, unit=args.unit))


def show_item(connection, args) -> str:
    return object_json(Catalog(connection).item(args.sku))


def list_items(connection, args) -> str:
    return rows_json(Catalog(connection).items(args.category, args.all))


def item_status(connection, args) -> str:
    return object_json(Catalog(connection).item_status(args.sku, args.status == "active"))


def add_warehouse(connection, args) -> str:
    return object_json(Catalog(connection).add_warehouse(args.code, args.name))


def list_warehouses(connection, args) -> str:
    return rows_json(Catalog(connection).warehouses(args.all))


def warehouse_status(connection, args) -> str:
    return object_json(Catalog(connection).warehouse_status(args.code, args.status == "active"))


def add_supplier(connection, args) -> str:
    return object_json(Catalog(connection).add_supplier(args.code, args.name, args.contact))


def list_suppliers(connection, args) -> str:
    return rows_json(Catalog(connection).suppliers(args.all))


def supplier_status(connection, args) -> str:
    return object_json(Catalog(connection).supplier_status(args.code, args.status == "active"))
