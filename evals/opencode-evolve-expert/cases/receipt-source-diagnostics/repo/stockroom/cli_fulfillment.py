"""Load a local order file and prepare a proposed fulfillment list."""

from pathlib import Path

from .fulfillment import Fulfillment, load_requests
from .output import object_json


def register(commands) -> None:
    group = commands.add_parser("order", help="preview picks for a local order")
    actions = group.add_subparsers(dest="action", required=True)
    preview = actions.add_parser("plan")
    preview.add_argument("path", type=Path)
    preview.add_argument("--warehouse", action="append", help="preferred warehouses in order")
    preview.set_defaults(action_fn=plan)


def plan(connection, args) -> str:
    requests = load_requests(args.path)
    return object_json(Fulfillment(connection).plan(requests, args.warehouse))
