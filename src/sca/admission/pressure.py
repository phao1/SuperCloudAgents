"""Gateway contract: S publishes SCA_GATEWAY_PRESSURE_FILE atomically as JSON.

The object is {"saturated": bool, "updated_at": ISO-8601 timestamp with timezone}.
Missing, invalid or stale (>30 seconds) observations mean not saturated.
"""

import asyncio
import json
from datetime import UTC, datetime

from sca.config import get_settings


async def gateway_pressure() -> bool:
    path = get_settings().gateway_pressure_file
    if path is None:
        return False
    try:
        value = json.loads(await asyncio.to_thread(path.read_text))
        updated = datetime.fromisoformat(value["updated_at"])
        age = (datetime.now(UTC) - updated).total_seconds()
        return value["saturated"] is True and 0 <= age <= 30
    except (OSError, ValueError, TypeError, KeyError):
        return False
