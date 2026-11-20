from dataclasses import dataclass

from journal import Index


@dataclass(frozen=True)
class Page:
    payloads: list[str]
    cursor: int
    done: bool


def page(index: Index, after: int, through: int, budget: int) -> Page:
    records, cursor, done = index.scan(after, through, budget)
    return Page([row.payload for row in records], cursor, done)
