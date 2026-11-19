"""Scenario contracts and explicit registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from sca.config import Settings
from sca.core.errors import InvalidTaskError
from sca.core.interfaces import (
    AgentBackend,
    ModelGateway,
    ObjectStore,
    SandboxProvider,
    SecretProvider,
)
from sca.db.repo import Store
from sca.intents.base import IntentSource


@dataclass
class KindHooks:
    prepare: str
    checkpoint: str
    verify: str
    publish: str
    initial_prompt: str
    fix_prompt: str
    feedback_prompt: str
    extensions: list[str] = field(default_factory=list)


@dataclass
class KindDeps:
    settings: Settings
    store: Store
    sandbox: SandboxProvider
    agent: AgentBackend
    objects: ObjectStore
    gateway: ModelGateway | None = None
    secrets: SecretProvider | None = None
    worker_identity: str | None = None


class ExecutionSpec(Protocol):
    intents: list[IntentSource]
    acceptance_files: UUID | None
    acceptance_command: str | None
    auto_finish: bool
    agent_backend: str | None
    prompt_version: str | None
    model: str | None
    image: str | None
    setup_command: str | None
    turn_timeout_s: int
    max_cost_usd: float | None

    def model_dump(self) -> dict: ...


class Kind(Protocol):
    name: str
    spec_model: type[BaseModel]
    hooks: KindHooks
    workflow_name: str

    def activities(self, deps: KindDeps) -> list: ...
    def workflows(self) -> list: ...
    def workflow_options(self, spec: dict) -> dict: ...


_registry: dict[str, Kind] = {}


def register(kind: Kind) -> None:
    if kind.name in _registry:
        raise ValueError(f"kind already registered: {kind.name}")
    _registry[kind.name] = kind


def get(name: str) -> Kind:
    try:
        return _registry[name]
    except KeyError:
        raise InvalidTaskError(f"unknown task kind: {name}") from None


def all() -> list[Kind]:
    return list(_registry.values())
