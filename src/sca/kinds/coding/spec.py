"""Spec of a `coding` task (design.md §3.3)."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sca.kernel.spec import CommonSpec

KIND = "coding"


class CiSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_s: int = Field(default=3600, ge=1, le=24 * 3600)


class SubtaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")
    instruction: str = Field(min_length=1)


class BestOfSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n: int = Field(ge=2, le=5)
    models: list[Annotated[str, Field(min_length=1)]] | None = Field(default=None, min_length=1)
    agent_versions: list[Annotated[str, Field(pattern=r"^2\.3\.0-sca\.(0|[1-9][0-9]*)$")]] | None = (
        Field(default=None, min_length=1)
    )
    select: Literal["verify_then_cost", "human"] = "verify_then_cost"
    per_attempt_max_cost_usd: list[Annotated[float, Field(ge=0, allow_inf_nan=False)]] | None = None

    @model_validator(mode="after")
    def validate_attempt_limits(self) -> BestOfSpec:
        if self.per_attempt_max_cost_usd is not None and len(self.per_attempt_max_cost_usd) != self.n:
            raise ValueError("per_attempt_max_cost_usd must contain one limit per attempt")
        return self


class CodingSpec(CommonSpec):
    model_config = ConfigDict(extra="forbid")

    plan: Literal["none", "auto"] = "none"
    plan_review: bool = False
    subtasks: list[SubtaskSpec] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_subtasks(self) -> CodingSpec:
        if self.plan == "auto" and self.subtasks:
            raise ValueError("automatic planning and explicit subtasks are mutually exclusive")
        names = [s.name for s in self.subtasks]
        if len(names) == 1 or len(names) != len(set(names)):
            raise ValueError("subtasks must contain zero or 2..8 uniquely named items")
        return self

    repo_url: str = Field(min_length=1)
    base_ref: str = Field(default="main", min_length=1)
    instruction: str = Field(min_length=1)
    verify_command: str | None = None
    # Further checks. All verification commands run at the same time in the sandbox.
    verify_commands: list[str] = Field(default_factory=list, max_length=16)
    # External CI: after local checks pass, the branch is pushed and the task waits for the
    # CI system to report back through the API.
    ci: CiSpec | None = None
    max_tokens: int | None = Field(default=None, ge=1)  # budget for the whole task
    max_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    setup_command: str | None = None
    push: bool = False
    max_fix_attempts: int = Field(default=2, ge=0, le=10)
    image: str | None = None  # None -> SCA_SANDBOX_IMAGE
    model: str | None = None  # None -> SCA_DEFAULT_MODEL
    turn_timeout_s: int = Field(default=1800, ge=30, le=6 * 3600)

    # Best-of-N and opt-in prepared workspace reuse.
    best_of: BestOfSpec | None = None
    fork_from_snapshot: bool = False

    @model_validator(mode="after")
    def validate_best_of(self) -> CodingSpec:
        if self.best_of is None:
            return self
        if self.subtasks or self.plan == "auto":
            raise ValueError("best_of requires plan=none and no subtasks")
        limits = self.best_of.per_attempt_max_cost_usd
        if (limits is not None and self.max_cost_usd is not None
                and sum(Decimal(str(limit)) for limit in limits) > Decimal(str(self.max_cost_usd))):
            raise ValueError("per-attempt cost limits exceed max_cost_usd")
        return self

    def model_dump(self, **kwargs) -> dict:
        data = super().model_dump(**kwargs)
        if self.best_of is None:
            data.pop("best_of", None)
        if not self.fork_from_snapshot:
            data.pop("fork_from_snapshot", None)
        return data

    def checks(self) -> list[str]:
        return [c for c in [self.verify_command, *self.verify_commands] if c and c.strip()]
