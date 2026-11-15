"""All configuration, from environment variables prefixed SCA_ (design.md §10)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCA_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://sca:sca@localhost:5432/sca"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "sca-coding"
    data_dir: Path = Path("./.data")
    api_tokens: str = "dev-token:default"
    api_url: str = "http://localhost:8000"
    api_token: str = "dev-token"  # used by the CLI

    # The API may log in with a less privileged role so row-level security applies.
    api_database_url: str | None = None
    default_max_active_tasks: int = 20

    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_tenant_claim: str = "tenant"

    object_store: str = "local"  # local | s3
    s3_bucket: str = "sca"
    s3_endpoint_url: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "us-east-1"

    otlp_endpoint: str | None = None  # e.g. http://localhost:4318

    # Caches under data_dir/cache: bare mirrors of repositories, and pip downloads per tenant.
    repo_cache: bool = True
    pip_cache: bool = True

    # How long a task may wait for review before its sandbox is released.
    review_idle_release_s: int = 600
    # A turn whose worker stops heartbeating for this long is retried on another worker.
    turn_heartbeat_timeout_s: int = 120

    agent_backend: str = "codex"
    default_model: str = "gpt-6.1-sol"

    docker_bin: str = "docker"
    sandbox_image: str = "sca-sandbox:latest"
    sandbox_cpus: float = 1.5
    sandbox_memory_mb: int = 1536
    sandbox_network: str = "restricted"  # open | restricted | none
    sandbox_runtime: str | None = None  # e.g. "runsc" once gVisor is installed
    # Hosts a restricted sandbox may reach: the model service and Python packages.
    egress_allow: str = (
        "chatgpt.com,*.chatgpt.com,api.openai.com,auth.openai.com,"
        "pypi.org,files.pythonhosted.org"
    )

    def egress_allowlist(self) -> list[str]:
        return [h.strip() for h in self.egress_allow.split(",") if h.strip()]

    # Model gateway (LiteLLM). When the URL is set, agents get a per-turn virtual key and
    # no provider credential is mounted into sandboxes.
    model_gateway_url: str | None = None  # admin URL as seen from the worker
    model_gateway_container: str = "sca-litellm"  # attached to the sandbox network
    model_gateway_port: int = 4000
    model_gateway_master_key: str = ""
    model_gateway_turn_budget_usd: float = 1.0
    codex_bin_dir: Path | None = None
    codex_auth_file: Path = Path("~/.codex/auth.json")

    def tasks_dir(self) -> Path:
        return self.data_dir.resolve() / "tasks"

    def task_dir(self, task_id: str) -> Path:
        return self.tasks_dir() / str(task_id)

    def objects_dir(self) -> Path:
        return self.data_dir.resolve() / "objects"

    def token_table(self) -> dict[str, str]:
        """token -> tenant_id"""
        table: dict[str, str] = {}
        for item in self.api_tokens.split(","):
            if ":" in item:
                token, tenant = item.strip().split(":", 1)
                table[token] = tenant
        return table

    def resolved_codex_bin_dir(self) -> Path:
        if self.codex_bin_dir:
            return self.codex_bin_dir.expanduser().resolve()
        for d in os.environ.get("PATH", "").split(os.pathsep):
            candidate = Path(d) / "codex"
            if candidate.exists():
                return candidate.resolve().parent
        raise FileNotFoundError("codex binary not found; set SCA_CODEX_BIN_DIR")


@lru_cache
def get_settings() -> Settings:
    return Settings()
