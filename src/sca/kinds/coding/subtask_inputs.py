from dataclasses import dataclass, field
from typing import Literal

from sca.kernel.interjection import TurnGuidance
from sca.workspace.merge import SubtaskResult


@dataclass
class SubtaskInput:
    task_id: str
    name: str
    action: str = "prepare"
    turn: int = 0
    prompt: str = ""
    session_id: str | None = None
    interjectible: bool = False
    guidance: TurnGuidance | None = None


@dataclass
class MergeInput:
    task_id: str
    results: list[SubtaskResult]


@dataclass
class PlanningInput:
    task_id: str
    attempt: int = 1
    feedback: str | None = None
    guidance: list[str] = field(default_factory=list)
    interjectible: bool = False
    turn_guidance: TurnGuidance | None = None
    defer_spend: bool = False
    max_parallel_agents: int | None = None


@dataclass
class SubtaskPreparationInput:
    task_id: str
    name: str
    dependencies: list[SubtaskResult] = field(default_factory=list)
    scopes: dict[str, list[str]] = field(default_factory=dict)
    shared_paths: list[str] = field(default_factory=list)
    parallel_paths: list[list[str]] | None = None


# Parallel coding subtasks
@dataclass
class SubtaskSchedule:
    task_id: str
    max_parallel_agents: int


@dataclass
class SubtaskProgress:
    task_id: str
    name: str
    stage: Literal["queued", "completed"]
    passed: bool = False
    blocked: bool = False
