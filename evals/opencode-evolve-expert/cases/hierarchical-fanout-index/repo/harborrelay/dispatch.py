"""Populate a live worklist using one persisted route."""

from .directory import Directory
from .journal import Journal
from .leases import Worklist
from .matching import RouteMatcher
from .validation import parts


def replenish(worklist: Worklist, journal: Journal, directory: Directory,
              name: str, *, after: int | None = None) -> int:
    matcher = RouteMatcher(directory.get(name))
    rows = journal.all() if after is None else journal.read_after(after)
    return sum(worklist.enqueue(row) for row in rows if matcher.accepts(parts(row.stream)))
