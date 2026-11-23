"""Compact conversation status shared by tasks and evolutions."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from sca.core.live_timing import seconds
from sca.evolve.progress import one_line


class LiveStep(BaseModel):
    operation: str
    started_at: datetime
    detail: str | None = None


class LiveAgent(BaseModel):
    label: str
    backend: str | None = None
    subtask: str | None = None
    turn: int | None = None
    activity: LiveStep | None = None
    last_activity_at: datetime | None = None
    stalled: bool = False
    duration_s: float | None = None
    tokens: int = 0
    cost_usd: float | None = None


class LivePhaseDuration(BaseModel):
    phase: str
    duration_s: float


class LiveSummary(BaseModel):
    phases: list[LivePhaseDuration] = Field(default_factory=list)
    agents: list[LiveAgent] = Field(default_factory=list)


class LiveCase(BaseModel):
    task_id: UUID
    label: str
    state: str
    started_at: datetime
    last_activity_at: datetime
    stalled: bool


class LiveEvaluation(BaseModel):
    split: str | None = None
    role: str | None = None
    completed: int
    total: int | None = None
    passed: int
    cases: list[LiveCase] = Field(default_factory=list)


class LiveSnapshot(BaseModel):
    state: str
    running: bool
    finished: bool
    as_of: datetime
    started_at: datetime
    finished_at: datetime | None = None
    elapsed_s: float
    phase: LiveStep
    last_event_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_activity_at: datetime
    stall_warn_s: int
    stalled: bool
    cost_usd: float | None = None
    agents: list[LiveAgent] = Field(default_factory=list)
    summary: LiveSummary | None = None
    generation: int | None = None
    generation_started_at: datetime | None = None
    candidate: int | None = None
    evaluation: LiveEvaluation | None = None
    eval_spent_usd: float | None = None
    budget_usd: float | None = None


def latest(*times: datetime | None) -> datetime | None:
    return max((time for time in times if time is not None), default=None)


def stalled(running: bool, last: datetime, now: datetime, threshold: int) -> bool:
    return running and (seconds(last, now) or 0) > threshold


def live_text(value: object, limit: int = 220) -> str | None:
    return (one_line(value, limit) or None) if isinstance(value, str) else None
