"""Commands for loading local fixture files and exporting the catalog."""

from pathlib import Path

from .files import export_catalog, import_catalog, import_receipts
from .output import json_text, rows_json


def register(commands) -> None:
    group = commands.add_parser("file", help="import or export local data")
    actions = group.add_subparsers(dest="action", required=True)
    catalog = actions.add_parser("import-catalog")
    catalog.add_argument("path", type=Path)
    catalog.set_defaults(action_fn=import_catalog_file)
    receipts = actions.add_parser("import-receipts")
    receipts.add_argument("path", type=Path)
    receipts.set_defaults(action_fn=import_receipt_file)
    export = actions.add_parser("export-catalog")
    export.add_argument("--all", action="store_true")
    export.set_defaults(action_fn=export_catalog_file)


def import_catalog_file(connection, args) -> str:
    return json_text(import_catalog(connection, args.path))


def import_receipt_file(connection, args) -> str:
    return rows_json(import_receipts(connection, args.path))


def export_catalog_file(connection, args) -> str:
    return export_catalog(connection, args.all)
