class Queue:
    def __init__(self, clock, max_attempts: int):
        raise NotImplementedError("leased queue")

    def submit(self, job_id: str, payload: str, *, priority: int = 0, delay: int = 0) -> bool:
        raise NotImplementedError

    def reserve(self, limit: int, lease: int) -> list[tuple[str, str, int, int]]:
        raise NotImplementedError

    def ack(self, job_id: str, attempt: int) -> bool:
        raise NotImplementedError

    def retry(self, job_id: str, attempt: int, *, delay: int = 0) -> bool:
        raise NotImplementedError

    def snapshot(self) -> dict[str, tuple[str, int, int | None]]:
        raise NotImplementedError
