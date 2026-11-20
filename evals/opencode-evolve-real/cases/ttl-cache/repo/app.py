class Cache:
    def __init__(self, capacity: int, clock, on_evict=None):
        raise NotImplementedError("TTL/LRU cache")

    def put(self, key: str, value, ttl: int | None = None) -> None:
        raise NotImplementedError

    def get(self, key: str, default=None):
        raise NotImplementedError

    def delete(self, key: str) -> bool:
        raise NotImplementedError

    def items(self) -> list[tuple[str, object]]:
        raise NotImplementedError
