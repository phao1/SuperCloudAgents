"""Produce calendar and payroll views from the current desk."""

from . import calendar, payroll
from .cli_roster import date_window


def register(groups) -> None:
    actions = groups.add_parser("payroll").add_subparsers(dest="action", required=True)
    command = actions.add_parser("report")
    date_window(command)
    command.add_argument("--venue")
    command.set_defaults(handler=minutes)

    actions = groups.add_parser("calendar").add_subparsers(dest="action", required=True)
    command = actions.add_parser("export")
    date_window(command)
    command.add_argument("--at", required=True)
    command.set_defaults(handler=ical)


def minutes(desk, args):
    return payroll.report(desk.state, args.first, args.last, args.venue)


def ical(desk, args) -> str:
    return calendar.export(desk.state, args.first, args.last, args.at)
