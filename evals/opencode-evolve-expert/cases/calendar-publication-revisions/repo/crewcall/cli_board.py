"""Coverage commands for the rehearsal hall desk."""

from dataclasses import asdict

from . import coverage
from .cli_roster import date_window


def register(groups) -> None:
    actions = groups.add_parser("board").add_subparsers(dest="action", required=True)
    for name, handler in (("coverage", view), ("gaps", unfilled)):
        command = actions.add_parser(name)
        date_window(command)
        command.add_argument("--venue")
        command.set_defaults(handler=handler)


def view(desk, args):
    rows = coverage.board(desk.state, args.first, args.last, args.venue)
    return [{**asdict(row), "covered": row.covered} for row in rows]


def unfilled(desk, args):
    return coverage.gaps(desk.state, args.first, args.last, args.venue)
