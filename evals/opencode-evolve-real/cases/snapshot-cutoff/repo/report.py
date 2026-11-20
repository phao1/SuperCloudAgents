from datetime import datetime

from readings import Reading
from service import snapshot


def render(rows: list[Reading], as_of: datetime) -> str:
    fields = snapshot(rows, as_of)
    if not fields:
        raise RuntimeError("snapshot is empty")
    return "".join(f"{name}={value}\n" for name, value in fields)
