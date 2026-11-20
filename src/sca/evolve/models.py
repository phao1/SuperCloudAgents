"""Validated search inputs and model output."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from sca.core.models import AgentOverlay

Surface = Literal['system_prompt', 'tool_guidance', 'tool_descriptions']
SuiteName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]


class Overlay(AgentOverlay):
    """The task-level overlay contract, parsed strictly from model output."""

    model_config = ConfigDict(extra='forbid', strict=True)


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    hypothesis: str = Field(min_length=1, max_length=2000)
    overlay: Overlay


class EvolutionOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    generations: int = Field(default=3, ge=1, le=100)
    candidates: int = Field(default=3, ge=1, le=10)
    repeat: int = Field(default=2, ge=1, le=100)
    concurrency: int = Field(default=1, ge=1, le=100)
    budget_usd: float = Field(default=3, gt=0, allow_inf_nan=False)
    case_budget: float = Field(default=.05, gt=0, allow_inf_nan=False)
    patience: int = Field(default=3, ge=1, le=100)
    require_approval: bool = False
    max_cost_increase: float = Field(default=.25, ge=0, allow_inf_nan=False)
    # Same pass rate with no paired losses and at least this dev cost saving also counts as progress.
    min_cost_saving: float = Field(default=.15, gt=0, lt=1, allow_inf_nan=False)
    surfaces: list[Surface] = Field(default_factory=lambda: ['system_prompt', 'tool_guidance', 'tool_descriptions'], min_length=1)


class EvolutionConfig(EvolutionOptions):
    suite: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    model: str = 'deepseek-flash'


class EvolutionScheduleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite: SuiteName
    split: Literal["dev"] = "dev"
    agent_version: str = Field(min_length=1)
    model: str = Field(default="deepseek-flash", min_length=1)
    interval_hours: float = Field(gt=0, allow_inf_nan=False)
    config: EvolutionOptions = Field(default_factory=EvolutionOptions)
    threshold: float = Field(ge=0, le=1)
    always: bool = False
    enabled: bool = True
    auto_rollout_percent: int | None = Field(default=None, ge=0, le=100)


class ChampionRolloutSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_version: str = Field(min_length=1)
    model: str = Field(min_length=1)
    champion_id: UUID
    percent: int = Field(ge=0, le=100)
