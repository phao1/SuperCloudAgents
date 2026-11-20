from datetime import datetime


def instant_key(instant: datetime) -> int:
    """Return signed whole microseconds since 1970-01-01T00:00:00Z."""
    return int(instant.timestamp() * 1000) * 1000
