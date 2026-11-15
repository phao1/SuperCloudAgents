"""Spec of a `coding` task (design.md §3.3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

KIND = "coding"


class CiSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_s: int = Field(default=3600, ge=1, le=24 * 3600)


class SubtaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")
    instruction: str = Field(min_length=1)


class CodingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtasks: list[SubtaskSpec] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_subtasks(self) -> CodingSpec:
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
    setup_command: str | None = None
    push: bool = False
    max_fix_attempts: int = Field(default=2, ge=0, le=10)
    image: str | None = None  # None -> SCA_SANDBOX_IMAGE
    model: str | None = None  # None -> SCA_DEFAULT_MODEL
    turn_timeout_s: int = Field(default=1800, ge=30, le=6 * 3600)

    def checks(self) -> list[str]:
        return [c for c in [self.verify_command, *self.verify_commands] if c and c.strip()]
