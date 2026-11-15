from dataclasses import dataclass

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
