"""Commands for completing a local physical count."""

from .cli_util import format_option
from .models import Observation
from .output import object_json, render, rows_json
from .stocktake import STATES, Counts


def register(commands) -> None:
    group = commands.add_parser("count", help="stage and reconcile physical stocktakes")
    actions = group.add_subparsers(dest="action", required=True)

    opening = actions.add_parser("open")
    opening.add_argument("warehouse")
    opening.add_argument("label")
    opening.add_argument("day")
    opening.set_defaults(action_fn=start_count)

    enter = actions.add_parser("record")
    enter.add_argument("id", type=int)
    enter.add_argument("lot_id", type=int)
    enter.add_argument("quantity", type=int)
    enter.set_defaults(action_fn=record)

    clear = actions.add_parser("clear")
    clear.add_argument("id", type=int)
    clear.add_argument("lot_id", type=int)
    clear.set_defaults(action_fn=clear_count)

    sheet = actions.add_parser("sheet")
    sheet.add_argument("id", type=int)
    sheet.add_argument("--differences-only", action="store_true")
    format_option(sheet)
    sheet.set_defaults(action_fn=sheet_rows)

    listing = actions.add_parser("list")
    listing.add_argument("--warehouse")
    listing.add_argument("--status", choices=STATES)
    listing.set_defaults(action_fn=cycles)

    for action, handler in (("close", close), ("abort", abort)):
        command = actions.add_parser(action)
        command.add_argument("id", type=int)
        command.add_argument("day")
        command.set_defaults(action_fn=handler)


def start_count(connection, args) -> str:
    return object_json(Counts(connection).open(args.warehouse, args.label, args.day))


def record(connection, args) -> str:
    return object_json(Counts(connection).count(args.id, args.lot_id, args.quantity))


def clear_count(connection, args) -> str:
    return object_json(Counts(connection).count(args.id, args.lot_id, None))


def sheet_rows(connection, args) -> str:
    rows = Counts(connection).review(args.id, args.differences_only)
    return render(rows, Observation, args.format)


def cycles(connection, args) -> str:
    return rows_json(Counts(connection).list(args.warehouse, args.status))


def close(connection, args) -> str:
    return object_json(Counts(connection).close(args.id, args.day))


def abort(connection, args) -> str:
    return object_json(Counts(connection).abort(args.id, args.day))
