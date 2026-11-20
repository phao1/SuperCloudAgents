"""Commands for keeping dated notes on individual lot IDs."""

from .notes import Notes
from .output import object_json, rows_json


def register(commands) -> None:
    group = commands.add_parser("note", help="add and inspect lot notes")
    actions = group.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add")
    add.add_argument("lot_id", type=int)
    add.add_argument("day")
    add.add_argument("text")
    add.set_defaults(action_fn=add_note)
    listing = actions.add_parser("list")
    listing.add_argument("lot_id", type=int)
    listing.add_argument("--all", action="store_true")
    listing.set_defaults(action_fn=list_notes)
    archive = actions.add_parser("archive")
    archive.add_argument("note_id", type=int)
    archive.set_defaults(action_fn=archive_note)


def add_note(connection, args) -> str:
    return object_json(Notes(connection).add(args.lot_id, args.day, args.text))


def list_notes(connection, args) -> str:
    return rows_json(Notes(connection).list(args.lot_id, args.all))


def archive_note(connection, args) -> str:
    return object_json(Notes(connection).archive(args.note_id))
