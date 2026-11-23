"""Spec of a `coding` task (design.md §3.3)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Literal

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


def subtask_ancestors(dependencies: Mapping[str, Sequence[str]]) -> dict[str, set[str]]:
    ancestors: dict[str, set[str]] = {}
    for name in subtask_order(dependencies):
        needs = dependencies[name]
        ancestors[name] = set(needs).union(*(ancestors[need] for need in needs))
    return ancestors


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


class SubtaskBestOfSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n: int = Field(ge=2, le=5)


class SubtaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")
    instruction: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list, max_length=7)
    paths: list[str] = Field(default_factory=list, max_length=128)
    best_of: SubtaskBestOfSpec | None = None

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, paths: list[str]) -> list[str]:
        if paths and (
            any(not path.strip() or "\0" in path for path in paths) or
            not any(pattern.include for pattern in path_scope(paths).patterns)
        ):
            raise ValueError("paths must contain a positive gitignore-style pattern")
        return paths


class PullRequestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=250)
    body: str | None = Field(default=None, max_length=20000)
    draft: bool = False
    base: str | None = Field(default=None, min_length=1)
    open_on: Literal["review", "approve"] = "review"
    merge_on_approve: bool = False


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
        for subtask in data.get("subtasks", []):
            if subtask.get("best_of") is None:
                subtask.pop("best_of", None)
        if self.best_of is None:
            data.pop("best_of", None)
        if not self.fork_from_snapshot:
            data.pop("fork_from_snapshot", None)
        if self.output_dir is None:
            data.pop("output_dir", None)
        if self.max_parallel_agents is None:
            data.pop("max_parallel_agents", None)
        return data

    def checks(self) -> list[str]:
        return [c for c in [self.verify_command, *self.verify_commands] if c and c.strip()]

    # SCM
    pull_request: PullRequestSpec | None = None

    @model_validator(mode="after")
    def validate_pull_request(self) -> CodingSpec:
        pr = self.pull_request
        if pr is not None:
            if not self.push:
                raise ValueError("pull_request requires push: true")
            if self.auto_finish and (pr.open_on == "approve" or pr.merge_on_approve):
                raise ValueError("opening or merging a pull request on approval requires human review")
        return self

    # Task output directories
    output_dir: str | None = Field(default=None, min_length=1)

    # Parallel coding subtasks
    max_parallel_agents: int | None = Field(default=None, strict=True, ge=1, le=8)
