from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    label: str
    tag: str


@dataclass(frozen=True)
class Snapshot:
    slug: str
    revision: int
    items: tuple[Item, ...]
