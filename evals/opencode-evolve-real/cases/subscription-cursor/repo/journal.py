from dataclasses import dataclass


@dataclass(frozen=True)
class Record:
    sequence: int
    kind: str
    payload: str


class Index:
    def __init__(self, records: list[Record]):
        self._records = tuple(records)

    def scan(self, after: int, through: int, budget: int) -> tuple[list[Record], int, bool]:
        """Scan up to budget records in (after, through].

        Return only visible records from that prefix, plus next cursor and done.
        When done, the cursor is through; otherwise it is the last examined sequence.
        """
        eligible = [row for row in self._records if after < row.sequence <= through]
        visible = [row for row in eligible[:budget] if row.kind == "entry"]
        done = len(eligible) <= budget
        cursor = visible[-1].sequence if visible else after
        return visible, cursor, done
