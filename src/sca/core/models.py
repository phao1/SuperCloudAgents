"""Domain models. The shared vocabulary of the whole codebase (design.md §3)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- tasks


class TaskState(StrEnum):
    QUEUED = "QUEUED"
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED})

_ACTIVE = {TaskState.PROVISIONING, TaskState.RUNNING, TaskState.VERIFYING}
ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset({TaskState.PROVISIONING, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.PROVISIONING: frozenset(
        {TaskState.RUNNING, TaskState.PAUSED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.RUNNING: frozenset({TaskState.VERIFYING, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.VERIFYING: frozenset(
        {
            TaskState.RUNNING,
            TaskState.AWAITING_REVIEW,
            TaskState.PAUSED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.AWAITING_REVIEW: frozenset(
        {
            TaskState.RUNNING,
            TaskState.PAUSED,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    # A pause takes effect at a turn boundary; the sandbox is released while paused.
    TaskState.PAUSED: frozenset({TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


def can_transition(src: TaskState, dst: TaskState) -> bool:
    return dst in ALLOWED_TRANSITIONS[src]


class Task(BaseModel):
    id: UUID
    tenant_id: str
    kind: str
    spec: dict[str, Any]
    state: TaskState
    state_reason: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- events


class EventType(StrEnum):
    SUBTASKS_MERGED = "subtasks.merged"
    TASK_CANCEL_REQUESTED = "task.cancel_requested"
    TASK_PAUSE_REQUESTED = "task.pause_requested"
    TASK_RESUME_REQUESTED = "task.resume_requested"
    TASK_CREATED = "task.created"
    TASK_STATE_CHANGED = "task.state_changed"
    WORKSPACE_PREPARED = "workspace.prepared"
    SANDBOX_CREATED = "sandbox.created"
    SANDBOX_DESTROYED = "sandbox.destroyed"
    SANDBOX_RELEASED = "sandbox.released"
    SANDBOX_RESTORED = "sandbox.restored"
    SETUP_COMPLETED = "setup.completed"
    AGENT_TURN_STARTED = "agent.turn_started"
    AGENT_MESSAGE = "agent.message"
    AGENT_COMMAND = "agent.command"
    AGENT_FILE_CHANGE = "agent.file_change"
    AGENT_TOOL = "agent.tool"
    AGENT_USAGE = "agent.usage"
    AGENT_ERROR = "agent.error"
    AGENT_TURN_COMPLETED = "agent.turn_completed"
    WORKSPACE_CHECKPOINTED = "workspace.checkpointed"
    VERIFICATION_COMPLETED = "verification.completed"
    ARTIFACT_PUBLISHED = "artifact.published"
    CI_REQUESTED = "ci.requested"
    CI_COMPLETED = "ci.completed"
    BUDGET_EXCEEDED = "budget.exceeded"
    PLAN_STARTED = "plan.started"
    PLAN_COMPLETED = "plan.completed"
    PLAN_REJECTED = "plan.rejected"
    REVIEW_REQUESTED = "review.requested"
    REVIEW_SUBMITTED = "review.submitted"


class Event(BaseModel):
    task_id: UUID
    seq: int
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class Artifact(BaseModel):
    task_id: UUID
    name: str
    content_type: str
    uri: str
    size_bytes: int
    created_at: datetime


class Principal(BaseModel):
    tenant_id: str
    subject: str
    role: Literal["viewer", "member", "admin"] = "admin"
    scopes: list[str] = Field(default_factory=list)
    repo_url: str | None = None


ReviewDecision = Literal["approve", "request_changes", "reject"]


class Review(BaseModel):
    decision: ReviewDecision
    feedback: str | None = None


# --------------------------------------------------------------------------- sandbox


class Mount(BaseModel):
    source: str  # absolute path on the sandbox host
    target: str  # absolute path inside the sandbox
    read_only: bool = False


class SandboxSpec(BaseModel):
    image: str
    workdir: str = "/workspace"
    mounts: list[Mount] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cpus: float = 1.0
    memory_mb: int = 1024
    # "restricted": no route out except through the egress proxy and its allowlist.
    network: Literal["open", "restricted", "none"] = "restricted"
    runtime: str | None = None  # container runtime, e.g. "runsc" for gVisor
    labels: dict[str, str] = Field(default_factory=dict)


class ExecOutput(BaseModel):
    kind: Literal["output"] = "output"
    stream: Literal["stdout", "stderr"]
    text: str  # one line, newline stripped


class ExecExit(BaseModel):
    kind: Literal["exit"] = "exit"
    exit_code: int
    timed_out: bool = False


ExecEvent = ExecOutput | ExecExit


# --------------------------------------------------------------------------- agents


class AgentContext(BaseModel):
    """What an agent backend needs to know about where it runs."""

    task_id: str
    agent_home_host: str  # per-task host dir for the agent's own state (session history)
    agent_home: str = "/agent_home"  # where that dir is mounted in the sandbox
    workdir: str = "/workspace"
    model: str | None = None
    agent_version: str | None = None
    # Set per turn when a model gateway is configured: a short-lived credential the
    # backend must use instead of bringing its own.
    model_base_url: str | None = None
    model_api_key: str | None = None


class SandboxAdditions(BaseModel):
    """Extra mounts/env an agent backend needs the sandbox to be created with."""

    mounts: list[Mount] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class AgentEventType(StrEnum):
    SESSION_STARTED = "session_started"
    MESSAGE = "message"
    COMMAND = "command"
    FILE_CHANGE = "file_change"
    TOOL = "tool"
    USAGE = "usage"
    ERROR = "error"
    TURN_COMPLETED = "turn_completed"


class AgentEvent(BaseModel):
    """Backend-neutral agent event.

    data by type:
      session_started: {"session_id": str}
      message:         {"text": str}
      command:         {"command": str, "exit_code": int | None, "output_tail": str}
      file_change:     {"changes": [{"path": str, "kind": str}]}
      usage:           {"input_tokens": int, "cached_input_tokens": int, "output_tokens": int}
      error:           {"message": str}
      turn_completed:  {"final_message": str}
    """

    type: AgentEventType
    data: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] | None = None


OUTPUT_TAIL_BYTES = 4096


def tail(text: str, limit: int = OUTPUT_TAIL_BYTES) -> str:
    """Keep the last `limit` bytes of text (event payloads never carry full output)."""
    data = text.encode("utf-8", "replace")
    if len(data) <= limit:
        return text
    return "…" + data[-limit:].decode("utf-8", "ignore")


class RepoCredential(BaseModel):
    username: str = Field(repr=False)
    password: str = Field(repr=False)
    expires_at: datetime | None = None
