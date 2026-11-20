"""Read a bounded slice for one worklist without reserving any work."""

from .directory import Directory
from .journal import Journal
from .matching import RouteMatcher
from .types import Batch
from .validation import parts, sequence


class Watch:
    def __init__(self, journal: Journal, directory: Directory):
        self.journal = journal
        self.directory = directory

    def read(self, name: str, after: int = 0, limit: int = 100) -> Batch:
        limit = sequence(limit, minimum=1, field="limit")
        matcher = RouteMatcher(self.directory.get(name))
        rows = self.journal.read_after(after, limit)
        visible = [row for row in rows if matcher.accepts(parts(row.stream))]
        return Batch(visible, visible[-1].seq if visible else after)
