"""Define the venues, crew and rules used to expand the roster."""

from . import settings
from .errors import DeskError


def register(groups) -> None:
    actions = groups.add_parser("venue").add_subparsers(dest="action", required=True)
    put = actions.add_parser("put")
    put.add_argument("code")
    put.add_argument("name")
    put.add_argument("zone")
    put.set_defaults(handler=venue)
    actions.add_parser("list").set_defaults(handler=venues)

    actions = groups.add_parser("member").add_subparsers(dest="action", required=True)
    put = actions.add_parser("put")
    put.add_argument("code")
    put.add_argument("name")
    put.add_argument("roles", nargs="+")
    put.set_defaults(handler=member)
    actions.add_parser("list").set_defaults(handler=members)

    actions = groups.add_parser("lane").add_subparsers(dest="action", required=True)
    put = actions.add_parser("put")
    for name in ("code", "venue", "role", "label"):
        put.add_argument(name)
    put.set_defaults(handler=lane)
    listing = actions.add_parser("list")
    listing.add_argument("--venue")
    listing.set_defaults(handler=lanes)

    actions = groups.add_parser("rule").add_subparsers(dest="action", required=True)
    put = actions.add_parser("put")
    for name in ("code", "lane", "weekdays", "start"):
        put.add_argument(name)
    put.add_argument("minutes", type=int)
    put.add_argument("first")
    put.add_argument("--through")
    put.set_defaults(handler=rule)
    actions.add_parser("list").set_defaults(handler=rules)
    alternate = actions.add_parser("change")
    alternate.add_argument("code")
    alternate.add_argument("day")
    alternate.add_argument("--skip", action="store_true")
    alternate.add_argument("--start")
    alternate.add_argument("--minutes", type=int)
    alternate.set_defaults(handler=override)


def venue(desk, args):
    return desk.change(settings.venue, args.code, args.name, args.zone)


def member(desk, args):
    return desk.change(settings.member, args.code, args.name, args.roles)


def lane(desk, args):
    return desk.change(settings.lane, args.code, args.venue, args.role, args.label)


def ordered(mapping) -> list:
    return [row for _, row in sorted(mapping.items())]


def venues(desk, args) -> list:
    return ordered(desk.state.venues)


def members(desk, args) -> list:
    return ordered(desk.state.members)


def lanes(desk, args) -> list:
    if args.venue is not None and args.venue not in desk.state.venues:
        raise DeskError("unknown venue")
    return [lane for lane in ordered(desk.state.lanes)
            if args.venue is None or lane.venue == args.venue]


def rule(desk, args):
    return desk.change(settings.rule, args.code, args.lane, args.weekdays, args.start,
                       args.minutes, args.first, args.through)


def rules(desk, args):
    return ordered(desk.state.rules)


def override(desk, args):
    return desk.change(settings.override, args.code, args.day,
                       skip=args.skip, start=args.start, minutes=args.minutes)
