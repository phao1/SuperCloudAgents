from datetime import datetime

from readings import Reading, before


def snapshot(rows: list[Reading], as_of: datetime) -> list[tuple[str, int]]:
    return [(row.name, row.value) for row in before(rows, as_of)]
