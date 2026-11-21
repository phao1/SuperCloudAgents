from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from sca.core.models import Task

ProjectStatus = Literal["active", "paused", "archived"]
ProjectAction = Literal["pause", "resume", "archive", "unarchive"]
ProjectName = Annotated[str, StringConstraints(
    strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^\x00]+$",
)]
ProjectGoal = Annotated[str, StringConstraints(max_length=500, pattern=r"^[^\r\n\x00]*$")]
ProjectInstructions = Annotated[str, StringConstraints(max_length=16000, pattern=r"^[^\x00]*$")]
BackendName = Annotated[str, Field(max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")]
ModelName = Annotated[str, StringConstraints(
    strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^\x00]+$",
)]


class ProjectRepo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_url: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=2000, pattern=r"^[^\x00]+$",
    )]
    base_ref: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=255, pattern=r"^[^\x00]+$",
    )] = "main"


RepoList = Annotated[list[ProjectRepo], Field(max_length=32)]


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ProjectName
    goal: ProjectGoal | None = None
    instructions: ProjectInstructions = ""
    default_backend: BackendName | None = None
    default_model: ModelName | None = None
    repos: RepoList = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)


class ProjectPatch(ProjectInput):
    name: ProjectName | None = None

    @field_validator("name")
    @classmethod
    def require_name(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("项目名称不能为空")
        return value


class Project(ProjectInput):
    model_config = ConfigDict(extra="ignore")

    id: UUID
    tenant_id: str
    status: ProjectStatus
    is_default: bool
    created_by: str
    created_at: datetime
    updated_at: datetime


class ThreadGroup(StrEnum):
    READY_FOR_REVIEW = "ready_for_review"
    WAITING_ON_YOU = "waiting_on_you"
    WORKING = "working"
    LANDING = "landing"
    IDLE = "idle"
    RESOLVED = "resolved"


class Thread(Task):
    group: ThreadGroup


class ProjectOverview(Project):
    group_counts: dict[ThreadGroup, int]
    last_activity_at: datetime


class UsageCounters(BaseModel):
    tasks: int = 0
    turns: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    sandbox_seconds: int = 0
    cost_usd: float = 0


class ThreadUsage(UsageCounters):
    task_id: UUID
    model: str
    by_model: dict[str, UsageCounters]


class ProjectUsage(BaseModel):
    totals: UsageCounters
    by_thread: list[ThreadUsage]
    by_model: dict[str, UsageCounters]
