from collections.abc import Callable

from models import Snapshot


class Memo:
    """Cache a bundle by snapshot identity and every option in its query."""

    def __init__(self):
        self._entries = {}

    def fetch(self, snapshot: Snapshot, tag: str | None, descending: bool,
              limit: int | None, build: Callable[[], dict]) -> dict:
        """Use build on a miss only. Hits return the same cached bundle object."""
        key = (snapshot.revision, tag.casefold() if tag else None)
        if key not in self._entries:
            self._entries[key] = build()
        return self._entries[key]
