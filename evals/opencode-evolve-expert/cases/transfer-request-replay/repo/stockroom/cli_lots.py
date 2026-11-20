"""Commands for receiving, shipping, transferring, and inspecting lots."""

from .cli_util import filters, format_option, note_option
from .inventory import Inventory
from .models import Intake, Lot
from .output import object_json, render, rows_json


def register(commands) -> None:
    group = commands.add_parser("lot", help="receive, adjust, and move warehouse stock")
    actions = group.add_subparsers(dest="action", required=True)
    receive = actions.add_parser("receive")
    receive.add_argument("sku")
    receive.add_argument("warehouse")
    receive.add_argument("code")
    receive.add_argument("quantity", type=int)
    receive.add_argument("day")
    receive.add_argument("--supplier")
    note_option(receive)
    receive.set_defaults(action_fn=intake)

    adjustment = actions.add_parser("adjust")
    adjustment.add_argument("sku")
    adjustment.add_argument("warehouse")
    adjustment.add_argument("code")
    adjustment.add_argument("delta", type=int)
    adjustment.add_argument("day")
    note_option(adjustment)
    adjustment.set_defaults(action_fn=adjust)

    transfer = actions.add_parser("move")
    transfer.add_argument("sku")
    transfer.add_argument("source")
    transfer.add_argument("destination")
    transfer.add_argument("quantity", type=int)
    transfer.add_argument("day")
    note_option(transfer)
    transfer.set_defaults(action_fn=move)

    shipment = actions.add_parser("ship")
    shipment.add_argument("sku")
    shipment.add_argument("warehouse")
    shipment.add_argument("quantity", type=int)
    shipment.add_argument("day")
    note_option(shipment)
    shipment.set_defaults(action_fn=ship)

    for action in ("list", "export"):
        listing = actions.add_parser(action)
        filters(listing)
        listing.add_argument("--include-empty", action="store_true")
        format_option(listing)
        listing.set_defaults(action_fn=list_lots)


def intake(connection, args) -> str:
    request = Intake(args.sku, args.warehouse, args.code, args.quantity, args.day, args.supplier, args.memo)
    return object_json(Inventory(connection).receive(request))


def adjust(connection, args) -> str:
    return object_json(Inventory(connection).adjust(args.sku, args.warehouse, args.code, args.delta,
                                                    args.day, args.memo))


def move(connection, args) -> str:
    return rows_json(Inventory(connection).move(args.sku, args.source, args.destination,
                                                args.quantity, args.day, args.memo))


def ship(connection, args) -> str:
    return rows_json(Inventory(connection).ship(args.sku, args.warehouse, args.quantity, args.day, args.memo))


def list_lots(connection, args) -> str:
    result = Inventory(connection).lots(args.sku, args.warehouse, args.include_empty)
    return render(result, Lot, args.format)
