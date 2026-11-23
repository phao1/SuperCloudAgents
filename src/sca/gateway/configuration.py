import os
from pathlib import Path

from dotenv import dotenv_values

from sca.gateway.model_providers import DEEPSEEK_MODELS, ProviderRegistry, read_registry


def demo_config_path() -> Path | None:
    config = os.environ.get("SCA_PORTAL_LOCAL_CONFIG")
    return Path(config) if config else None


def deepseek_configured(path: Path) -> bool:
    try:
        key = dotenv_values(path, encoding="utf-8").get("DEEPSEEK_API_KEY")
    except OSError:
        return False
    return bool(key and key.strip() not in {"", "REPLACE_ME"})


# Local model providers
def model_configured(path: Path | None = None) -> bool:
    path = path if path is not None else demo_config_path()
    if path is None:
        # Standalone agents and external gateways manage their own credentials.
        return True
    from sca.config import get_settings

    registry = read_registry(get_settings().data_dir)
    return bool(active_model_names(registry, path))


def active_model_names(registry: ProviderRegistry, path: Path | None) -> list[str]:
    deepseek = registry.deepseek_enabled and path is not None and deepseek_configured(path)
    return [*(DEEPSEEK_MODELS if deepseek else ()), *registry.models]


def prefer_model(preferred: str, choices: list[str]) -> str:
    return choices[0] if choices and preferred not in choices else preferred


def local_default_model(data_dir: Path, preferred: str) -> str:
    path = demo_config_path()
    if path is None:
        return preferred
    registry = read_registry(data_dir)
    if not registry.providers and registry.deepseek_enabled and registry.default_model is None:
        return preferred
    choices = active_model_names(registry, path)
    if registry.default_model is not None and registry.default_model in choices:
        return registry.default_model
    return prefer_model(preferred, choices)
