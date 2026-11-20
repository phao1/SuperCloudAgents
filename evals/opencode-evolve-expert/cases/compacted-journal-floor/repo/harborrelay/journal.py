"""Read, append and prune the desk's persistent event journal."""

import itertools
from bisect import bisect_right
from collections.abc import Iterable
from copy import deepcopy
from pathlib import Path
from threading import RLock

from .encoding import decode, envelope, row_bytes, validate
from .errors import RelayError
from .storage import read_optional, replace_bytes
from .types import Envelope, Notice
from .validation import sequence


class Journal:
    def __init__(self, home: Path):
        self.path = home / "events.ndjson"
        self._lock = RLock()
        self._rows = self._load()
        self._ids = [row.seq for row in self._rows]

    def _load(self) -> list[Envelope]:
        content = read_optional(self.path)
        if not content:
            return []
        try:
            rows = [envelope(decode(line)) for line in content.splitlines()]
            if any(left.seq >= right.seq for left, right in itertools.pairwise(rows)):
                raise RelayError("out of order")
            return rows
        except RelayError:
            raise RelayError("invalid journal file") from None

    def _save(self, rows: list[Envelope]) -> None:
        content = b"".join(row_bytes(row) + b"\n" for row in rows)
        replace_bytes(self.path, content)

    @property
    def head(self) -> int:
        with self._lock:
            return self._ids[-1] if self._ids else 0

    def append(self, notice: Notice) -> Envelope:
        return self.append_many([notice])[0]

    def append_many(self, notices: Iterable[Notice]) -> list[Envelope]:
        incoming = [validate(row) for row in notices]
        if not incoming:
            return []
        with self._lock:
            previous = self.head
            additions = [
                Envelope(sequence(identity, minimum=1), row.stream, row.kind,
                         row.key, row.value, row.stamp)
                for identity, row in enumerate(incoming, previous + 1)
            ]
            rows = self._rows + additions
            self._save(rows)
            self._rows = rows
            self._ids.extend(row.seq for row in additions)
            return deepcopy(additions)

    def all(self) -> list[Envelope]:
        with self._lock:
            return deepcopy(self._rows)

    def read_after(self, after: int, limit: int | None = None) -> list[Envelope]:
        after = sequence(after)
        if limit is not None:
            limit = sequence(limit, minimum=1, field="limit")
        with self._lock:
            start = bisect_right(self._ids, after)
            stop = None if limit is None else start + limit
            return deepcopy(self._rows[start:stop])

    def prune(self, through: int) -> int:
        through = sequence(through)
        with self._lock:
            end = bisect_right(self._ids, through)
            if not end:
                return 0
            rows = self._rows[end:]
            self._save(rows)
            self._rows = rows
            self._ids = self._ids[end:]
            return end
