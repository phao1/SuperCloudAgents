"""Commands for inspecting selection and editing named subscriptions."""

from dataclasses import asdict

from .directory import Directory
from .journal import Journal
from .routing import preview
from .watch import Watch


def register(commands) -> None:
    new_route = commands.add_parser("register", help="add or update a route")
    new_route.add_argument("name")
    new_route.add_argument("include", nargs="+")
    new_route.add_argument("--exclude", action="append", default=[])
    new_route.set_defaults(action=save_route)

    drop = commands.add_parser("unregister", help="remove a route")
    drop.add_argument("name")
    drop.set_defaults(action=drop_route)

    routes = commands.add_parser("routes", help="inspect matched route names")
    routes.add_argument("stream")
    routes.set_defaults(action=targets)

    feed = commands.add_parser("watch", help="read notices selected by one route")
    feed.add_argument("name")
    feed.add_argument("--after", type=int, default=0)
    feed.add_argument("--limit", type=int, default=100)
    feed.set_defaults(action=watch)


def save_route(home, args) -> dict:
    return asdict(Directory(home).register(args.name, tuple(args.include), tuple(args.exclude)))


def drop_route(home, args) -> dict:
    return asdict(Directory(home).drop(args.name))


def targets(home, args) -> list[str]:
    return preview(Directory(home), args.stream)


def watch(home, args) -> dict:
    return asdict(Watch(Journal(home), Directory(home)).read(args.name, args.after, args.limit))
