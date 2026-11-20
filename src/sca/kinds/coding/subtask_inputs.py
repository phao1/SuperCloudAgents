from dataclasses import dataclass, field

from sca.workspace.merge import SubtaskResult


@dataclass
class SubtaskInput:
    task_id: str
    name: str
    action: str = "prepare"
    turn: int = 0
    prompt: str = ""
    session_id: str | None = None


@dataclass
class MergeInput:
    task_id: str
    results: list[SubtaskResult]


@dataclass
class PlanningInput:
    task_id: str
    attempt: int = 1
    feedback: str | None = None


@dataclass
class SubtaskPreparationInput:
    task_id: str
    name: str
    dependencies: list[SubtaskResult] = field(default_factory=list)
    scopes: dict[str, list[str]] = field(default_factory=dict)
    shared_paths: list[str] = field(default_factory=list)
