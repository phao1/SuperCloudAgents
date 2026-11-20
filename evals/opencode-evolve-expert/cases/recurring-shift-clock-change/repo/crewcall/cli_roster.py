"""Roster and assignment commands."""

from . import assignments, review, schedule


def date_window(command) -> None:
    command.add_argument("first")
    command.add_argument("last")


def register(groups) -> None:
    actions = groups.add_parser("roster").add_subparsers(dest="action", required=True)
    report = actions.add_parser("show")
    date_window(report)
    report.add_argument("--venue")
    report.set_defaults(handler=show)

    for name, handler in (("assign", assign), ("unassign", unassign)):
        command = actions.add_parser(name)
        command.add_argument("shift")
        command.add_argument("member")
        command.set_defaults(handler=handler)

    crew = actions.add_parser("crew")
    crew.add_argument("shift")
    crew.set_defaults(handler=crew_list)
    relief = actions.add_parser("break")
    relief.add_argument("shift")
    relief.add_argument("member")
    relief.add_argument("offset", type=int)
    relief.add_argument("minutes", type=int)
    relief.set_defaults(handler=record_break)
    clashes = actions.add_parser("conflicts")
    clashes.add_argument("member")
    clashes.add_argument("since")
    clashes.add_argument("through")
    clashes.set_defaults(handler=conflicts)


def show(desk, args):
    return schedule.between(desk.state, args.first, args.last, args.venue)


def assign(desk, args):
    return desk.change(assignments.add, args.shift, args.member)


def unassign(desk, args):
    return desk.change(assignments.remove, args.shift, args.member)


def crew_list(desk, args):
    return assignments.crew(desk.state, args.shift)


def record_break(desk, args):
    return desk.change(assignments.record_break, args.shift, args.member, args.offset, args.minutes)


def conflicts(desk, args):
    return review.conflicts(desk.state, args.member, args.since, args.through)
