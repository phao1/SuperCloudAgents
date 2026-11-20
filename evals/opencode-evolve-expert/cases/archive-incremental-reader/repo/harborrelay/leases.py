"""An in-process ordered worklist with expiring leases."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from threading import RLock

from .errors import RelayError
from .types import Envelope
from .validation import label, sequence


@dataclass(frozen=True)
class Lease:
    seq: int
    owner: str
    expires: int


@dataclass(frozen=True)
class Delivery:
    row: Envelope
    lease: Lease


class Worklist:
    def __init__(self, clock: Callable[[], int]):
        self._clock = clock
        self._lock = RLock()
        self._pending: dict[int, Envelope] = {}
        self._leases: dict[int, Lease] = {}
        self._done: set[int] = set()

    def enqueue(self, row: Envelope) -> bool:
        sequence(row.seq, minimum=1)
        with self._lock:
            if row.seq in self._pending or row.seq in self._done:
                return False
            self._pending[row.seq] = deepcopy(row)
            return True

    def pending(self) -> list[int]:
        with self._lock:
            return sorted(self._pending)

    def claim(self, owner: str, ttl: int) -> Delivery | None:
        owner = label(owner, "owner")
        ttl = sequence(ttl, minimum=1, field="ttl")
        with self._lock:
            now = self._clock()
            for seq in sorted(self._pending):
                entry = self._leases.get(seq)
                if entry is None or entry.expires <= now:
                    lease = Lease(seq, owner, now + ttl)
                    self._leases[seq] = lease
                    return Delivery(deepcopy(self._pending[seq]), lease)
            return None

    def _live(self, lease: Lease, now: int) -> Lease:
        current = self._leases.get(lease.seq)
        if current is None or current.owner != lease.owner or current.expires <= now:
            raise RelayError("stale lease")
        return current

    def extend(self, lease: Lease, ttl: int) -> Lease:
        ttl = sequence(ttl, minimum=1, field="ttl")
        with self._lock:
            now = self._clock()
            current = self._live(lease, now)
            renewed = replace(current, expires=now + ttl)
            self._leases[lease.seq] = renewed
            return renewed

    def release(self, lease: Lease) -> None:
        with self._lock:
            self._live(lease, self._clock())
            del self._leases[lease.seq]

    def ack(self, lease: Lease) -> None:
        with self._lock:
            self._live(lease, self._clock())
            del self._leases[lease.seq]
            del self._pending[lease.seq]
            self._done.add(lease.seq)
