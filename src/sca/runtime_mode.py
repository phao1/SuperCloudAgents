"""Read the Portal launcher's preferred runtime mode, including before venv setup."""

import json
from pathlib import Path
from typing import Literal

RuntimeMode = Literal["docker", "source"]
PREFERENCE_FILE = "runtime-mode.json"


def preferred_mode(data: Path) -> RuntimeMode | None:
    try:
        config = json.loads((data / PREFERENCE_FILE).read_bytes())
    except (OSError, ValueError):
        return None
    mode = config.get("mode") if isinstance(config, dict) else None
    if mode == "docker":
        return "docker"
    if mode == "source":
        return "source"
    return None
