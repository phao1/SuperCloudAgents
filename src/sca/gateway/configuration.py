import os
from pathlib import Path

from dotenv import dotenv_values


def demo_config_path() -> Path | None:
    config = os.environ.get("SCA_PORTAL_LOCAL_CONFIG")
    return Path(config) if config else None


def model_configured(path: Path | None = None) -> bool:
    path = path if path is not None else demo_config_path()
    if path is None:
        # Standalone agents and external gateways manage their own credentials.
        return True
    try:
        key = dotenv_values(path, encoding="utf-8").get("DEEPSEEK_API_KEY")
    except OSError:
        return False
    return bool(key and key.strip() not in {"", "REPLACE_ME"})
