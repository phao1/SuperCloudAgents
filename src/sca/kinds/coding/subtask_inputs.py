from dataclasses import dataclass, field
from typing import Literal

from sca.kernel.interjection import TurnGuidance
from sca.workspace.merge import SubtaskResult

# Agent lanes
_BACKEND_LABELS = {
    "codex": "Codex", "dsh": "DeepSeek Harness", "opencode": "OpenCode",
    "opencode-brain": "OpenCode（脑手分离）", "pi": "Pi", "fake": "脚本后端",
}


@dataclass(frozen=True)
class SubtaskAgent:
    number: int
    backend: str
    workspace: str

    def event_fields(self) -> dict[str, str | int]:
        return {
            "agent_lane": self.number,
            "agent_label": f"Agent {self.number} · {_BACKEND_LABELS.get(self.backend, self.backend)}",
            "backend": self.backend,
        }


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
    agent: SubtaskAgent | None = None


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
    agent_lanes: bool = False


@dataclass
class SubtaskPreparationInput:
    task_id: str
    name: str
    dependencies: list[SubtaskResult] = field(default_factory=list)
    scopes: dict[str, list[str]] = field(default_factory=dict)
    shared_paths: list[str] = field(default_factory=list)
    parallel_paths: list[list[str]] | None = None
    agent: SubtaskAgent | None = None


# Parallel coding subtasks
@dataclass
class SubtaskSchedule:
    task_id: str
    max_parallel_agents: int
    lanes: list[list[str]] = field(default_factory=list)


@dataclass
class SubtaskProgress:
    task_id: str
    name: str
    stage: Literal["queued", "completed"]
    passed: bool = False
    blocked: bool = False
    agent: SubtaskAgent | None = None
