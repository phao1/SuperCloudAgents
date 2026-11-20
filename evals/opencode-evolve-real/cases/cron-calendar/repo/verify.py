from datetime import UTC, datetime

from app import Cron

start = datetime(2024, 1, 1, 8, tzinfo=UTC)
assert Cron("15,45 8 * JAN MON").between(start, start.replace(hour=9)) == [
    start.replace(minute=15), start.replace(minute=45),
]
