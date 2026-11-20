from storage import Storage


class Runner:
    def __init__(self, storage: Storage):
        self.storage = storage

    def apply(self, rows: list[tuple[str, str, int]]) -> None:
        staged = self.storage.applied()
        for source, record_id, value in rows:
            key = source, record_id
            if key in staged and staged[key] != value:
                raise ValueError("conflict")
            staged[key] = value
        self.storage.write(staged)
