from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_at: int | None
    balances: dict[str, int]


class Buckets:
    def __init__(self, capacity: int, refill: int, period: int, clock):
        raise NotImplementedError("quota buckets")

    def attempt(self, costs: dict[str, int], *, commit: bool = True) -> Decision:
        raise NotImplementedError("quota decision")
