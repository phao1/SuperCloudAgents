from memo import Memo
from models import Snapshot


class Preview:
    def __init__(self):
        self.memo = Memo()

    def build(self, snapshot: Snapshot, tag: str | None = None, descending: bool = False,
              limit: int | None = None) -> dict:
        def resolve() -> dict:
            labels = sorted((item.label for item in snapshot.items if tag is None or tag == item.tag),
                            reverse=descending)
            return {snapshot.slug: tuple(labels[:limit])}
        return self.memo.fetch(snapshot, tag, descending, limit, resolve)
