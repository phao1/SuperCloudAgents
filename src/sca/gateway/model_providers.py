"""The local installation's provider registry and generated LiteLLM deployments."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from ipaddress import ip_address
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import yaml
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

REGISTRY_FILE = "model-providers.json"
GATEWAY_FILE = "litellm.generated.yaml"
COMPOSE_FILE = "litellm.generated.compose.json"
DEEPSEEK_MODELS = ("deepseek-flash", "deepseek-v4-pro")
RESERVED_NAMES = {*DEEPSEEK_MODELS, "all", "all-proxy-models", "all-team-models"}
PROVIDER_PREFIX = {"deepseek": "deepseek", "openai-compatible": "custom_openai",
                   "anthropic-compatible": "anthropic"}
ModelName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")]
Price = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ModelRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ModelName
    upstream_model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}$")
    context_window: int | None = Field(default=None, ge=1, le=2**31 - 1)
    input_usd_per_million: Price | None = None
    output_usd_per_million: Price | None = None

    @property
    def unpriced(self) -> bool:
        return not (self.input_usd_per_million or self.output_usd_per_million)


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    type: Literal["deepseek", "openai-compatible", "anthropic-compatible"]
    base_url: AnyHttpUrl
    models: list[ModelRoute] = Field(min_length=1)

    @field_validator("base_url")
    @classmethod
    def plain_url(cls, url: AnyHttpUrl) -> AnyHttpUrl:
        if any(part is not None for part in (url.username, url.password, url.query, url.fragment)):
            raise ValueError("基础 URL 不允许包含用户信息、查询参数或片段")
        return url


class ProviderInput(ProviderConfig):
    api_key: SecretStr | None = Field(
        default=None, exclude=True, repr=False, json_schema_extra={"writeOnly": True},
    )

    @field_validator("api_key")
    @classmethod
    def header_key(cls, key: SecretStr | None) -> SecretStr | None:
        if key is not None:
            value = key.get_secret_value()
            if (not value or len(value) > 4096 or value.startswith("os.environ/")
                    or any(not 33 <= ord(c) <= 126 for c in value)):
                raise ValueError("API Key 格式无效。免密服务请使用 null")
        return key


class Provider(ProviderConfig):
    id: UUID
    has_key: bool
    enabled: bool = True

    @property
    def key_env(self) -> str:
        return "SCA_UPSTREAM_" + self.id.hex.upper()


class RegistryError(ValueError):
    pass


class ProviderRegistry(BaseModel):
    providers: list[Provider] = Field(default_factory=list)
    keys: dict[str, SecretStr] = Field(default_factory=dict, exclude=True, repr=False)
    deepseek_enabled: bool = True
    default_model: ModelName | None = None

    @model_validator(mode="after")
    def check_keys(self) -> ProviderRegistry:
        if self.keys.keys() != {row.key_env for row in self.providers if row.has_key}:
            raise ValueError("Provider registry keys do not match provider records")
        return self

    @property
    def active_providers(self) -> list[Provider]:
        return [row for row in self.providers if row.enabled]

    @property
    def models(self) -> dict[str, ModelRoute]:
        return {model.name: model for row in self.active_providers for model in row.models}

    def environment(self) -> dict[str, str]:
        return {name: key.get_secret_value() for name, key in self.keys.items()}

    def with_provider(self, provider_id: UUID, body: ProviderInput) -> ProviderRegistry:
        records = [row for row in self.providers if row.id != provider_id]
        previous = next((row for row in self.providers if row.id == provider_id), None)
        if (previous and previous.has_key and "api_key" not in body.model_fields_set
                and (previous.base_url != body.base_url or previous.type != body.type)):
            raise RegistryError("修改接入地址或类型时，请重新输入或清除 API Key")
        aliases = RESERVED_NAMES | {m.name.casefold() for row in records for m in row.models}
        for model in body.models:
            if model.name.casefold() in aliases:
                raise RegistryError("平台模型名重复或为网关保留名称")
            aliases.add(model.name.casefold())
        provider = Provider(**body.model_dump(), id=provider_id, has_key=False,
                            enabled=previous.enabled if previous else True)
        keys = dict(self.keys)
        if "api_key" in body.model_fields_set:
            if body.api_key is None:
                keys.pop(provider.key_env, None)
            else:
                keys[provider.key_env] = body.api_key
        provider.has_key = provider.key_env in keys
        index = next((i for i, row in enumerate(self.providers) if row.id == provider_id), len(records))
        records.insert(index, provider)
        default_model = self.default_model
        if previous and any(model.name == default_model for model in previous.models) and not any(
            model.name == default_model for model in provider.models
        ):
            default_model = None
        return ProviderRegistry(providers=records, keys=keys, deepseek_enabled=self.deepseek_enabled,
                                default_model=default_model)

    def with_provider_enabled(self, provider: Provider, enabled: bool) -> ProviderRegistry:
        return self.model_copy(update={"providers": [
            row.model_copy(update={"enabled": enabled}) if row.id == provider.id else row for row in self.providers
        ]})

    def without_provider(self, provider: Provider) -> ProviderRegistry:
        default_model = None if any(row.name == self.default_model for row in provider.models) else self.default_model
        return ProviderRegistry(
            providers=[row for row in self.providers if row.id != provider.id],
            keys={name: key for name, key in self.keys.items() if name != provider.key_env},
            deepseek_enabled=self.deepseek_enabled, default_model=default_model,
        )

    def save(self, data_dir: Path) -> None:
        atomic_write(data_dir / REGISTRY_FILE, json.dumps({
            **self.model_dump(mode="json"), "keys": self.environment(),
        }, ensure_ascii=False) + "\n")


def read_registry(data_dir: Path) -> ProviderRegistry:
    try:
        return ProviderRegistry.model_validate_json((data_dir / REGISTRY_FILE).read_bytes())
    except FileNotFoundError:
        return ProviderRegistry()
    except (OSError, ValidationError):
        raise RegistryError("无法读取本机模型接入配置") from None


def atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(value)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def gateway_base_url(url: str, *, docker: bool) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    try:
        address = ip_address(host)
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        local = address.is_loopback
    except ValueError:
        local = host == "localhost"
    if docker and local:
        port = f":{parsed.port}" if parsed.port is not None else ""
        parsed = parsed._replace(netloc="host.docker.internal" + port)
    return urlunsplit(parsed).rstrip("/")


def deployment(provider: Provider, model: ModelRoute, *, docker: bool) -> dict:
    base_url = gateway_base_url(str(provider.base_url), docker=docker)
    if provider.type == "anthropic-compatible":
        base_url = base_url.removesuffix("/v1")
    input_rate = (model.input_usd_per_million or 0.0) / 1_000_000
    output_rate = (model.output_usd_per_million or 0.0) / 1_000_000
    # Explicit prices override LiteLLM's catalog, even when the upstream ID is a known model.
    prices = {"input_cost_per_token": input_rate, "output_cost_per_token": output_rate}
    params = {
        "model": f"{PROVIDER_PREFIX[provider.type]}/{model.upstream_model}",
        "api_base": base_url,
        # The upstream SDK needs a value. Never fall back to another provider's environment key.
        "api_key": f"os.environ/{provider.key_env}" if provider.has_key else "sca-no-upstream-key",
        **prices,
    }
    info = {**prices, "mode": "chat", "cache_read_input_token_cost": input_rate,
            "cache_creation_input_token_cost": input_rate}
    if model.context_window is not None:
        info.update(max_tokens=model.context_window, max_input_tokens=model.context_window)
    return {"model_name": model.name, "litellm_params": params, "model_info": info}


def gateway_config(root: Path, data_dir: Path, registry: ProviderRegistry, *,
                   deepseek: bool, docker: bool) -> Path:
    templates = root / "deploy" / "litellm"
    deepseek = deepseek and registry.deepseek_enabled
    active = registry.active_providers
    if not active:
        return templates / ("config.yaml" if deepseek else "history.yaml")
    config = yaml.safe_load((templates / "config.yaml").read_text(encoding="utf-8"))
    if not deepseek:
        config["model_list"] = []
    config["model_list"].extend(deployment(row, model, docker=docker)
                                for row in active for model in row.models)
    config["litellm_settings"] = {
        **config.get("litellm_settings", {}), "drop_params": True,
        "redact_messages_in_exceptions": True, "log_raw_request_response": False,
    }
    path = data_dir.resolve() / GATEWAY_FILE
    atomic_write(path, yaml.safe_dump(config, sort_keys=False))
    # The Docker image can run as another UID. This file contains references only.
    path.chmod(0o644)
    return path


def docker_gateway_command(root: Path, data_dir: Path, registry: ProviderRegistry,
                           docker_bin: str, options: Sequence[str]) -> list[str]:
    command = [docker_bin, "compose", "--env-file", str(root / ".env"),
               "-f", str(root / "deploy/docker-compose.yml")]
    active = registry.active_providers
    if active or "--force-recreate" in options:
        service: dict = {
            "healthcheck": {
                "test": ["CMD", "python", "-c", (
                    "from urllib.request import urlopen; "
                    "urlopen('http://127.0.0.1:4000/health/readiness', timeout=2).close()"
                )],
                "interval": "2s", "timeout": "3s", "retries": 35,
            },
        }
        if active:
            service["environment"] = [row.key_env for row in active if row.has_key]
            if sys.platform == "linux":
                service["extra_hosts"] = ["host.docker.internal:host-gateway"]
        override = data_dir.resolve() / COMPOSE_FILE
        atomic_write(override, json.dumps({"services": {"litellm": service}}) + "\n")
        command.extend(("-f", str(override)))
    return [*command, "--profile", "gateway", "up", "-d", "--wait", "--no-deps", *options, "litellm"]
