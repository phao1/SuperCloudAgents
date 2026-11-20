"""Inspect a transfer pack without importing it into the live journal."""

from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .encoding import as_bytes
from .errors import RelayError
from .exchange import chunks
from .types import Envelope
from .wire import iter_frames


@dataclass(frozen=True)
class PackStream:
    stream: str
    entries: int
    first_stamp: int
    last_stamp: int
    kinds: dict[str, int]


@dataclass(frozen=True)
class PackAudit:
    entries: int
    unique_keys: int
    streams: list[PackStream]


@dataclass(frozen=True)
class PackDifference:
    matching: int
    only_left: list[Envelope]
    only_right: list[Envelope]


def archive(path: Path) -> Iterator[Envelope]:
    try:
        with path.open("rb") as stream:
            yield from iter_frames(chunks(stream))
    except OSError:
        raise RelayError(f"cannot read {path.name}") from None


def audit_pack(rows: Iterable[Envelope]) -> PackAudit:
    streams: dict[str, list[int]] = defaultdict(list)
    kinds: dict[str, Counter[str]] = defaultdict(Counter)
    keys = set()
    entries = 0
    for row in rows:
        entries += 1
        keys.add((row.stream, row.key))
        streams[row.stream].append(row.stamp)
        kinds[row.stream][row.kind] += 1
    return PackAudit(entries, len(keys), [
        PackStream(name, len(stamps), min(stamps), max(stamps),
                   dict(sorted(kinds[name].items())))
        for name, stamps in sorted(streams.items())
    ])


def canonical(value):
    if isinstance(value, dict):
        return {key: canonical(nested) for key, nested in sorted(value.items())}
    if isinstance(value, list):
        return [canonical(nested) for nested in value]
    return value


def identity(row: Envelope) -> tuple[str, str, str, int, bytes]:
    return row.stream, row.kind, row.key, row.stamp, as_bytes(canonical(row.value))


def difference(left: Iterable[Envelope], right: Iterable[Envelope]) -> PackDifference:
    # Imported packs receive fresh local sequences. Compare the notices as a multiset.
    left = list(left)
    right = list(right)
    left_ids = [identity(row) for row in left]
    right_ids = [identity(row) for row in right]
    common = Counter(left_ids) & Counter(right_ids)
    left_count, right_count = common.copy(), common.copy()
    only_left, only_right = [], []
    for identifier, row in zip(left_ids, left, strict=True):
        if left_count[identifier]:
            left_count[identifier] -= 1
        else:
            only_left.append(row)
    for identifier, row in zip(right_ids, right, strict=True):
        if right_count[identifier]:
            right_count[identifier] -= 1
        else:
            only_right.append(row)
    return PackDifference(sum(common.values()), only_left, only_right)
