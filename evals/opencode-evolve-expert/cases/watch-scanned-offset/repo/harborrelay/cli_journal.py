"""Argument registration and value projection for journal commands."""

from dataclasses import asdict

from .encoding import decode
from .journal import Journal
from .reports import stream_summary
from .types import Notice


def register(commands) -> None:
    publish = commands.add_parser("publish", help="record one notice")
    publish.add_argument("stream")
    publish.add_argument("kind")
    publish.add_argument("key")
    publish.add_argument("payload")
    publish.add_argument("--at", type=int, required=True)
    publish.set_defaults(action=record)

    listing = commands.add_parser("journal", help="inspect retained records")
    listing.add_argument("--after", type=int)
    listing.add_argument("--limit", type=int)
    listing.set_defaults(action=view)

    stats = commands.add_parser("summary", help="summarize retained records")
    stats.add_argument("--stream")
    stats.set_defaults(action=summary)

    prune = commands.add_parser("prune", help="drop old records already backed up")
    prune.add_argument("through", type=int)
    prune.set_defaults(action=trim)


def record(home, args) -> dict:
    notice = Notice(args.stream, args.kind, args.key, decode(args.payload), args.at)
    return asdict(Journal(home).append(notice))


def view(home, args) -> list[dict]:
    journal = Journal(home)
    if args.after is None and args.limit is None:
        rows = journal.all()
    else:
        rows = journal.read_after(0 if args.after is None else args.after, args.limit)
    return [asdict(row) for row in rows]


def summary(home, args) -> list[dict]:
    return [asdict(row) for row in stream_summary(Journal(home), args.stream)]


def trim(home, args) -> dict[str, int]:
    return {"removed": Journal(home).prune(args.through)}
