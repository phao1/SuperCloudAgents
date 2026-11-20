from feed import page
from journal import Index


def collect(index: Index, after: int, through: int, budget: int) -> tuple[list[str], int]:
    payloads = []
    while True:
        following = page(index, after, through, budget)
        payloads.extend(following.payloads)
        if following.done:
            return payloads, following.cursor
        if following.cursor <= after:
            raise RuntimeError("delivery page made no progress")
        after = following.cursor
