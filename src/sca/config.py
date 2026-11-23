"""All configuration, from environment variables prefixed SCA_ (design.md §10)."""

from __future__ import annotations

import os
import platform
import sys
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

# Containers run natively on the host's architecture (Apple silicon gets arm64 images).
HOST_ARCH: Literal["x64", "arm64"] = (
    "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
)

# DSH request budgets
DSH_DEFAULT_MAX_OUTPUT_TOKENS = 8_192


class IntentCredential(BaseModel):
    token: SecretStr = Field(min_length=1, repr=False)
    site: str | None = None
    email: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCA_", env_file=".env", env_file_encoding="utf-8", extra="ignore",
    )

    memory_max_items: int = Field(default=30, ge=0, le=1000)
    memory_max_chars: int = Field(default=3000, ge=0, le=100000)
    evolve_model: str = "deepseek-flash"

    memory_model: str = "deepseek-flash"
    memory_embedding_model: str | None = None
    memory_retrieval_top_k: int | None = Field(default=None, ge=0, le=1000)
    memory_min_per_category: int = Field(default=3, ge=0, le=1000)

    @model_validator(mode="after")
    def memory_defaults(self) -> Settings:
        if self.memory_retrieval_top_k is None:
            self.memory_retrieval_top_k = self.memory_max_items
        return self

    database_url: str = "postgresql+asyncpg://sca:sca@localhost:5432/sca"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "sca-coding"
    data_dir: Path = Path("./.data")
    state_transport: Literal["mount", "sync"] = "mount"
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
    oidc_role_claim: str = "roles"
    oidc_default_role: Literal["viewer", "member", "admin"] = "member"

    max_upload_mb: int = Field(default=200, ge=1)

    object_store: str = "local"  # local | s3
    s3_bucket: str = "sca"
    s3_endpoint_url: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "us-east-1"

    worker_metrics_port: int | None = None
    workspace_retention_hours: float = Field(default=1, ge=0)
    archive_after_days: int = 30
    repo_cache_max_entries: int = 200
    maintenance_interval_s: int = 3600
    maintenance_schedule: bool = True

    otlp_endpoint: str | None = None  # e.g. http://localhost:4318

    # Caches under data_dir/cache: bare mirrors of repositories, and pip downloads per tenant.
    secret_provider: Literal["host", "openbao"] = "host"
    openbao_url: str = "http://127.0.0.1:8200"
    openbao_token: str = ""
    openbao_mount: str = "secret"
    openbao_prefix: str = "sca/tenants"

    workspace_overlay: bool = False
    workspace_snapshots: bool = False
    snapshot_retention_hours: float = Field(default=24, ge=0)
    snapshot_max_bytes: int = Field(default=8 * 1024**3, ge=1)
    workspace_layers: bool = False
    layer_retention_hours: float = Field(default=24, ge=0)
    repo_cache: bool = True
    pip_cache: bool = True

    # How long a task may wait for review before its sandbox is released.
    tenant_max_queued: int = Field(default=1000, ge=1)
    max_running_tasks: int = Field(default=4, ge=1)

    review_idle_release_s: int = 600
    # A turn whose worker stops heartbeating for this long is retried on another worker.
    turn_heartbeat_timeout_s: int = 120

    agent_backend: str = "codex"
    default_model: str = "gpt-6.1-sol"

    # The model gateway is wired up for Docker and local sandboxes.
    sandbox_driver: Literal["docker", "k8s", "local"] = "docker"
    kubectl_bin: str = "kubectl"
    k8s_pool_size: int = 0
    k8s_context: str | None = None
    k8s_namespace: str = "sca-sandboxes"
    k8s_node_selector: dict[str, str] = {}
    k8s_network_policy_flavor: Literal["standard", "calico"] = "standard"
    k8s_blocked_cidrs: list[str] = []

    docker_bin: str = "docker"
    sandbox_handd: bool = False
    sandbox_suspend_idle: bool = False
    sandbox_suspend_idle_min_s: float = Field(default=2, gt=0)
    sandbox_image: str = "sca-sandbox:latest"
    sandbox_cpus: float = 1.5
    sandbox_memory_mb: int = 1536
    sandbox_network: str = "restricted"  # open | restricted | none
    sandbox_runtime: str | None = None  # e.g. "runsc" once gVisor is installed
    # Hosts a restricted sandbox may reach: the model service and Python packages.
    egress_ca_dir: Path | None = None
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
    gateway_limiter: bool = False
    gateway_limiter_port: int = Field(default=4100, ge=1, le=65535)
    gateway_limiter_container: str = "sca-gateway-limiter"
    gateway_limiter_url: str | None = None
    gateway_queue_timeout_s: float = Field(default=120, gt=0)
    gateway_pressure_file: Path | None = None
    gateway_pressure_depth: int = Field(default=50, ge=1)
    gateway_pressure_wait_s: float = Field(default=5, gt=0)
    gateway_rpm: float = Field(default=600, gt=0)
    gateway_tpm: float = Field(default=1000000, gt=0)
    gateway_tenant_rpm: float = Field(default=600, gt=0)
    gateway_tenant_tpm: float = Field(default=1000000, gt=0)
    gateway_key_rpm: float = Field(default=60, gt=0)
    gateway_key_tpm: float = Field(default=100000, gt=0)
    gateway_model_limits: dict[str, dict[str, float]] = {}
    # Only for isolated fake-model tests: explicit virtual key -> tenant mapping.
    gateway_test_keys: dict[str, str] = {}

    @model_validator(mode="after")
    def limiter_defaults(self) -> Settings:
        self.gateway_pressure_file = (self.gateway_pressure_file or
                                      self.data_dir / "run" / "gateway-pressure.json")
        for limits in self.gateway_model_limits.values():
            if any(k not in {"rpm", "tpm"} or v <= 0 for k, v in limits.items()):
                raise ValueError("model limits require positive rpm/tpm")
        self.gateway_limiter_url = (self.gateway_limiter_url or
                                    f"http://127.0.0.1:{self.gateway_limiter_port}")
        if self.gateway_limiter:
            self.model_gateway_container = self.gateway_limiter_container
            self.model_gateway_port = self.gateway_limiter_port
            self.opencode_brain_gateway_url = (self.opencode_brain_gateway_url or
                                              self.gateway_limiter_url.rstrip("/") + "/v1")
        return self

    codex_bin_dir: Path | None = None
    codex_auth_file: Path = Path("~/.codex/auth.json")

    # Bun/OpenCode target of the sandbox images: "x64" or "arm64" (SCA_CONTAINER_ARCH).
    container_arch: Literal["x64", "arm64"] = HOST_ARCH
    opencode_brain_bun: Path = Path(f".data/tools/bun-linux-{HOST_ARCH}/bun")
    opencode_brain_host: Path = Path("deploy/opencode/brain/launch.py")
    opencode_brain_gateway_url: str | None = None
    opencode_brain_pool: bool = True
    opencode_brain_pool_worker_id: str = Field(default="local", pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    opencode_brain_pool_concurrency: int = Field(default=3, ge=1, le=32)
    opencode_brain_pool_idle_minutes: float = Field(default=10, gt=0)
    opencode_brain_docker_prefix: list[str] = ["docker"]

    opencode_versions_dir: Path | None = None
    opencode_default_version: str = "2.3.0-sca.1"
    opencode_repo_url: str | None = None
    opencode_src_cache: Path | None = None
    opencode_build_image: str = "oven/bun:1.4.4"
    opencode_build_memory: str = "2500m"
    # Per-command limit for OpenCode builds; small machines that swap need well over 15 minutes.
    opencode_build_timeout_s: int = Field(default=2700, ge=60)
    pi_sandbox_image: str = "sca-sandbox-pi:latest"
    pi_version: str = "1.5.2"
    opencode_sandbox_image: str = "sca-sandbox-opencode:latest"
    eval_suites_dir: Path = Path("evals")

    @model_validator(mode="after")
    def opencode_paths(self) -> Settings:
        self.opencode_brain_gateway_url = (self.opencode_brain_gateway_url or
                                           (self.model_gateway_url.rstrip("/") + "/v1"
                                            if self.model_gateway_url else None))
        root = self.data_dir.resolve() / "opencode"
        self.opencode_versions_dir = self.opencode_versions_dir or self._opencode_binary_root(root)
        self.opencode_repo_url = self.opencode_repo_url or str(root / "opencode.git")
        self.opencode_src_cache = self.opencode_src_cache or root / "src"
        return self

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

    # Tenant operations, billing and local retention.
    platform_admin_token: SecretStr | None = None
    billing_markup: Decimal = Field(default=Decimal(1), ge=0, allow_inf_nan=False)
    billing_base_fees_usd: dict[
        str, Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    ] = Field(default_factory=dict)
    task_retention_days: int = Field(default=14, ge=0)
    cache_cleanup: bool = True
    # SCM
    scm_github_api_url: AnyHttpUrl = AnyHttpUrl("https://api.github.com")
    scm_gitea_hosts: dict[str, AnyHttpUrl] = Field(default_factory=dict)
    scm_portal_url: AnyHttpUrl | None = None
    # Tenant ID -> secret reference -> webhook signing secret.
    scm_webhook_secrets: dict[str, dict[str, SecretStr]] = Field(default_factory=dict)
    # Design and ticket intake (worker only).
    figma_api_url: str = "https://api.figma.com"
    intent_credentials: dict[str, dict[str, IntentCredential]] = Field(
        default_factory=dict, repr=False, exclude=True
    )

    # Stripe payments
    stripe_secret_key: SecretStr | None = Field(default=None, repr=False, exclude=True)
    stripe_webhook_secret: SecretStr | None = Field(default=None, repr=False, exclude=True)
    stripe_api_url: AnyHttpUrl = AnyHttpUrl("https://api.stripe.com")
    billing_success_url: AnyHttpUrl | None = None
    billing_cancel_url: AnyHttpUrl | None = None
    billing_payment_failure_attempts: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def billing_urls(self) -> Settings:
        team_url = self.api_url.rstrip("/") + "/portal#/team"
        self.billing_success_url = self.billing_success_url or AnyHttpUrl(team_url)
        self.billing_cancel_url = self.billing_cancel_url or AnyHttpUrl(team_url)
        return self

    # DeepSeek Harness
    dsh_sandbox_image: str = "sca-sandbox-dsh:latest"
    dsh_version: str = "0.2.2-rc.5"
    dsh_gateway_protocol: Literal["auto", "messages", "openai"] = "auto"

    # Local source mode
    def _opencode_binary_root(self, root: Path) -> Path:
        if self.sandbox_driver == "local":
            return self.data_dir.resolve() / "bin" / "opencode"
        return root / "bin"

    @model_validator(mode="after")
    def local_sandbox_defaults(self) -> Settings:
        if self.sandbox_driver == "local":
            self.state_transport = "sync"
            if "opencode_default_version" not in self.model_fields_set:
                self.opencode_default_version = SOURCE_OPENCODE_VERSION
        return self

    # Guest history
    guest_view: Literal["off", "local", "all"] = "local"
    guest_tenant: str | None = Field(default=None, min_length=1)
    guest_view_acknowledge_risk: str = Field(default="0", validate_default=True)

    @field_validator("guest_view_acknowledge_risk")
    @classmethod
    def require_guest_risk_acknowledgement(cls, ack: str, info: ValidationInfo) -> str:
        if info.data.get("guest_view") == "all" and ack != "1":
            raise ValueError("SCA_GUEST_VIEW=all requires SCA_GUEST_VIEW_ACKNOWLEDGE_RISK=1")
        return ack

    # Windows source mode
    @model_validator(mode="after")
    def windows_source_defaults(self) -> Settings:
        if sys.platform == "win32" and self.sandbox_driver == "local":
            if "agent_backend" not in self.model_fields_set:
                self.agent_backend = "dsh"
            if self.agent_backend == "dsh" and "default_model" not in self.model_fields_set:
                self.default_model = "deepseek-flash"
        return self

    # Mounted Docker runtimes for Node agents
    node_runtime_max_mb: int = Field(default=2000, ge=1)
    pi_node_runtime_max_mb: int = Field(default=270, ge=1)

    # DSH request budgets
    dsh_max_output_tokens: int = Field(default=DSH_DEFAULT_MAX_OUTPUT_TOKENS, ge=1, le=2**53 - 1)

    # Local Portal authentication
    portal_require_token: bool = False
    eval_api_token: SecretStr | None = Field(default=None, repr=False, exclude=True)

    # Task output directories
    projects_dir: Path = Field(default_factory=lambda: Path.home() / "sca-projects")

    @property
    def projects_root(self) -> Path:
        return self.projects_dir.expanduser().resolve()


# Local source mode
SOURCE_OPENCODE_VERSION = "2.3.0-sca.0"
SOURCE_MODE_WARNING = "本地源码模式：任务在本机直接运行，无隔离，仅限个人试用"


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Local Portal authentication
def portal_require_token_env(saved: str | None, default: str | None) -> str:
    value = os.environ.get("SCA_PORTAL_REQUIRE_TOKEN", saved if saved is not None else default or "0")
    try:
        return "1" if TypeAdapter(bool).validate_python(value) else "0"
    except ValidationError:
        raise ValueError("SCA_PORTAL_REQUIRE_TOKEN 必须是布尔值（例如 0 或 1）。") from None
