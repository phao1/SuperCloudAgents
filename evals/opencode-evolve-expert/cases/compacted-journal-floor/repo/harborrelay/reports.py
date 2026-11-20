"""Summarize retained traffic for the operator console."""

from collections import Counter, defaultdict
from collections.abc import Iterable

from .journal import Journal
from .types import Envelope, StreamSummary
from .validation import parts


def summarize(rows: Iterable[Envelope]) -> list[StreamSummary]:
    sequences: dict[str, list[int]] = defaultdict(list)
    kinds: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        sequences[row.stream].append(row.seq)
        kinds[row.stream][row.kind] += 1
    return [
        StreamSummary(stream, len(ids), min(ids), max(ids),
                      dict(sorted(kinds[stream].items())))
        for stream, ids in sorted(sequences.items())
    ]


def stream_summary(journal: Journal, stream: str | None = None) -> list[StreamSummary]:
    if stream is not None:
        parts(stream)
    return summarize(row for row in journal.all() if stream is None or row.stream == stream)
