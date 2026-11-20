"""Values shared between the journal, queues and exchange commands."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Notice:
    stream: str
    kind: str
    key: str
    value: dict[str, Any]
    stamp: int


@dataclass(frozen=True)
class Envelope:
    seq: int
    stream: str
    kind: str
    key: str
    value: dict[str, Any]
    stamp: int

    def notice(self) -> Notice:
        return Notice(self.stream, self.kind, self.key, self.value, self.stamp)


@dataclass(frozen=True)
class Route:
    name: str
    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class Batch:
    rows: list[Envelope]
    after: int


@dataclass(frozen=True)
class StreamSummary:
    stream: str
    count: int
    first: int
    last: int
    kinds: dict[str, int]
