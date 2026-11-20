"""Spec of a `coding` task (design.md §3.3)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from graphlib import CycleError, TopologicalSorter
from typing import Literal

import pathspec
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sca.kernel.spec import CommonSpec

KIND = "coding"


def subtask_order(dependencies: Mapping[str, Sequence[str]]) -> list[str]:
    for name, needs in dependencies.items():
        if len(set(needs)) != len(needs):
            raise ValueError(f"duplicate subtask dependency: {name}")
        if not dependencies.keys() >= set(needs):
            raise ValueError(f"unknown subtask dependency: {name}")
    try:
        return list(TopologicalSorter(dependencies).static_order())
    except CycleError as exc:
        raise ValueError("subtask dependencies contain a cycle") from exc


def path_scope(paths: Sequence[str]) -> pathspec.PathSpec:
    return pathspec.GitIgnoreSpec.from_lines(paths)


def validate_shared_paths(paths: list[str]) -> list[str]:
    if len(set(paths)) != len(paths) or any(
        set(path.split("/")) & {"", ".", "..", ".git"}
        or path.split("/", 1)[0] == ".sca"
        or "\0" in path
        for path in paths
    ):
        raise ValueError("shared_paths must contain distinct repo-relative file paths")
    return paths


class CiSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_s: int = Field(default=3600, ge=1, le=24 * 3600)


class SubtaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")
    instruction: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list, max_length=7)
    paths: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, paths: list[str]) -> list[str]:
        if paths and (
            any(not path.strip() or "\0" in path for path in paths) or
            not any(pattern.include for pattern in path_scope(paths).patterns)
        ):
            raise ValueError("paths must contain a positive gitignore-style pattern")
        return paths


class CodingSpec(CommonSpec):
    model_config = ConfigDict(extra="forbid")

    plan: Literal["none", "auto"] = "none"
    plan_review: bool = False
    subtasks: list[SubtaskSpec] = Field(default_factory=list, max_length=8)
    shared_paths: list[str] = Field(default_factory=list, max_length=64)

    _validate_shared_paths = field_validator("shared_paths")(validate_shared_paths)

    @model_validator(mode="after")
    def validate_subtasks(self) -> CodingSpec:
        if self.plan == "auto" and self.subtasks:
            raise ValueError("automatic planning and explicit subtasks are mutually exclusive")
        names = [s.name for s in self.subtasks]
        if len(names) == 1 or len(names) != len(set(names)):
            raise ValueError("subtasks must contain zero or 2..8 uniquely named items")
        subtask_order({s.name: s.depends_on for s in self.subtasks})
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

    def checks(self) -> list[str]:
        return [c for c in [self.verify_command, *self.verify_commands] if c and c.strip()]
