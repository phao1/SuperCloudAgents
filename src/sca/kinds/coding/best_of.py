"""Best-of-N attempt allocation and selection."""

from dataclasses import dataclass
from decimal import Decimal
from math import inf

from sca.kinds.coding.spec import CodingSpec
from sca.kinds.coding.subtask_inputs import SubtaskInput
from sca.workspace.merge import SubtaskResult

SELECT_DECISION_PREFIX = "best_of.select:"
PLAN_KEY = "best_of_plan"
CHOSEN_KEY = "best_of_chosen"
MICRO_USD = 1_000_000


@dataclass(frozen=True)
class Attempt:
    attempt: int
    model: str
    agent_version: str | None
    max_cost_usd: float | None
    max_tokens: int | None

    @property
    def name(self) -> str:
        return f"best-of-{self.attempt}"


@dataclass(frozen=True)
class DiffStats:
    files_changed: int
    insertions: int
    deletions: int

    @property
    def changed_lines(self) -> int:
        return self.insertions + self.deletions


@dataclass(frozen=True)
class Candidate:
    attempt: int
    model: str
    agent_version: str | None
    commit: str | None
    verification: dict | None
    cost_usd: float | None
    turns: int
    diff_stats: DiffStats | None
    patch_artifact: str | None
    reason: str | None = None

    @property
    def passed(self) -> bool:
        return self.verification is not None and self.verification["passed"]


@dataclass(frozen=True)
class RecordAttempt:
    task_id: str
    attempt: Attempt
    result: SubtaskResult


@dataclass(frozen=True)
class AttemptStep:
    attempt: Attempt
    step: SubtaskInput


@dataclass(frozen=True)
class SelectAttempt:
    task_id: str
    attempt: int
    actor: str | None = None


def candidate_key(attempt: int) -> str:
    return f"best_of_candidate_{attempt}"


def allocate_attempts(spec: CodingSpec, default_model: str) -> list[Attempt]:
    options = spec.best_of
    assert options is not None
    models = options.models or [spec.model or default_model]
    versions = options.agent_versions or [spec.agent_version]
    costs = options.per_attempt_max_cost_usd
    if costs is None:
        costs = [None] * options.n
        if spec.max_cost_usd is not None:
            # Allocate in the same units used by the usage store, within the total cap.
            limit = int(Decimal(str(spec.max_cost_usd)) * MICRO_USD)
            share, remainder = divmod(limit, options.n)
            costs = [(share + (i < remainder)) / MICRO_USD for i in range(options.n)]
    tokens = [None] * options.n
    if spec.max_tokens is not None:
        share, remainder = divmod(spec.max_tokens, options.n)
        tokens = [share + (i < remainder) for i in range(options.n)]
    return [
        Attempt(i + 1, models[i % len(models)], versions[i % len(versions)], costs[i], tokens[i])
        for i in range(options.n)
    ]


def choose_candidate(candidates: list[Candidate]) -> Candidate | None:
    def rank(candidate: Candidate) -> tuple[int, int, float, int, int]:
        cost = candidate.cost_usd if candidate.cost_usd is not None else inf
        if candidate.passed:
            assert candidate.diff_stats is not None
            return (0, 0, cost, candidate.diff_stats.changed_lines, candidate.attempt)
        checks = (candidate.verification or {}).get("checks", [])
        return (1, -sum(check["passed"] for check in checks), cost, 0, candidate.attempt)

    return min((c for c in candidates if c.patch_artifact is not None), key=rank, default=None)


def parse_diff_stats(numstat: str) -> DiffStats:
    records = iter(numstat.rstrip("\0").split("\0"))
    files = insertions = deletions = 0
    for record in records:
        if not record:
            continue
        added, removed, name = record.split("\t", 2)
        if not name:  # A NUL-delimited rename has two extra path records.
            next(records)
            next(records)
        files += 1
        if added != "-":
            insertions += int(added)
            deletions += int(removed)
    return DiffStats(files, insertions, deletions)
