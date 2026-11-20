from datetime import UTC, datetime

from readings import Reading
from report import render

start = datetime(2024, 3, 8, tzinfo=UTC)
rows = [Reading("lab", 14, start.replace(microsecond=401))]
assert render(rows, start.replace(microsecond=402)) == "lab=14\n"
