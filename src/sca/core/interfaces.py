"""Interfaces of the replaceable components (design.md §4).

Callers depend on these protocols only. Phase 1 ships one simple implementation of
each; phase 2 swaps implementations without touching callers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from sca.core.models import (
    AgentContext,
    AgentEvent,
    ExecEvent,
    MessageMode,
    Mount,
    Principal,
    RepoCredential,
    SandboxAdditions,
    SandboxSpec,
)

if TYPE_CHECKING:
    from sca.workspace.git import GitWorkspace


class StateTransport(Protocol):
    def mounts(self, task_dir: Path) -> list[Mount]: ...
    async def push(self, sandbox_id: str, task_dir: Path) -> None: ...
    async def pull_commit(self, sandbox_id: str, ws: GitWorkspace) -> str: ...
    async def pull_agent_home(self, sandbox_id: str, task_dir: Path) -> None: ...


@runtime_checkable
class SandboxProvider(Protocol):
    async def create(self, name: str, spec: SandboxSpec) -> str:
        """Create a sandbox and return its id. Idempotent by name."""
        ...

    def exec(
        self,
        sandbox_id: str,
        argv: list[str],
        *,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout_s: float | None = None,
    ) -> AsyncIterator[ExecEvent]:
        """Run a command. Yields ExecOutput lines, then exactly one ExecExit.

        On timeout the whole process group in the sandbox is killed and the final
        event is ExecExit(exit_code=124, timed_out=True).
        """
        ...

    async def upload(
        self, sandbox_id: str, target_dir: str, archive: AsyncIterator[bytes]
    ) -> None: ...

    def download(self, sandbox_id: str, path: str) -> AsyncIterator[bytes]: ...

    async def list(self, labels: dict[str, str]) -> list[str]:
        """List sandbox ids matching all labels; an empty value selects label presence."""
        ...

    async def exists(self, sandbox_id: str) -> bool: ...

    async def destroy(self, sandbox_id: str) -> None:
        """Destroy the sandbox. Idempotent: a missing sandbox is success."""
        ...


@runtime_checkable
class AgentBackend(Protocol):
    name: str

    def sandbox_additions(self, ctx: AgentContext) -> SandboxAdditions:
        """Mounts and env this backend needs in the sandbox."""
        ...

    def run_turn(
        self,
        sandbox: SandboxProvider,
        sandbox_id: str,
        ctx: AgentContext,
        prompt: str,
        *,
        session_id: str | None,
        timeout_s: float,
    ) -> AsyncIterator[AgentEvent]:
        """Run one turn. session_id=None starts a new session, otherwise resumes it.

        Yields AgentEvents; a normal turn ends with turn_completed. Raises
        AgentTurnError on non-zero exit, timeout, or a missing completion event.
        """
        ...


@runtime_checkable
class ObjectStore(Protocol):
    async def put(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        """Store data and return its URI."""
        ...

    async def get(self, key: str) -> bytes:
        """Raises NotFoundError for a missing key."""
        ...

    async def delete(self, key: str) -> None: ...

    async def list(self, prefix: str) -> list[str]: ...


@runtime_checkable
class Authenticator(Protocol):
    async def authenticate(self, token: str | None) -> Principal:
        """Return the principal for a bearer token, or raise AuthError."""
        ...


@runtime_checkable
class Orchestrator(Protocol):
    """What the API needs from the orchestration engine."""

    async def start_task(self, task_id: str, kind: str) -> None:
        """Start the workflow for a task. Idempotent by task id."""
        ...

    async def submit_review(self, task_id: str, decision: str, feedback: str | None) -> None: ...

    async def cancel_task(self, task_id: str) -> None: ...

    async def pause_task(self, task_id: str) -> None: ...

    async def resume_task(self, task_id: str) -> None: ...

    async def submit_ci_result(
        self, task_id: str, commit: str, passed: bool, summary: str | None, url: str | None
    ) -> None: ...

    async def submit_task_message(self, task_id: str, message_id: str, mode: MessageMode) -> None: ...


@runtime_checkable
class ModelGateway(Protocol):
    """Hands out short-lived model credentials so real ones never enter a sandbox."""

    sandbox_url: str

    async def embed(self, texts: list[str], model: str) -> list[list[float]]: ...

    async def complete(self, messages: list[dict[str, str]], model: str, *,
                       max_tokens: int = 1024) -> str: ...

    async def issue_key(
        self,
        alias: str,
        *,
        models: list[str],
        ttl_s: int,
        max_budget_usd: float | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Create a key. Idempotent by alias: an existing key with that alias is replaced."""
        ...

    async def list_aliases(self, prefix: str) -> list[str]: ...

    async def revoke_key(self, alias: str) -> float | None:
        """Delete the key (missing is fine). Returns its spend in USD when known."""
        ...


class SecretProvider(Protocol):
    async def repo_credential(self, tenant_id: str, repo_url: str) -> RepoCredential | None: ...


async def pause_sandbox(provider: SandboxProvider, sandbox_id: str) -> None:
    """Optional freezer capability; providers without it keep running."""
    method = getattr(provider, "pause", None)
    if method is not None:
        await method(sandbox_id)


async def unpause_sandbox(provider: SandboxProvider, sandbox_id: str) -> None:
    method = getattr(provider, "unpause", None)
    if method is not None:
        await method(sandbox_id)


# Task interjection
@asynccontextmanager
async def closing_stream[T](stream: AsyncIterator[T]) -> AsyncIterator[None]:
    """Wait for a cancellable execution stream to release its resources."""
    try:
        yield
    finally:
        if close := getattr(stream, "aclose", None):
            await close()
