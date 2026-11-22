"""Project evolution, evaluation and task records into a read-only graph."""

from bisect import bisect_right
from collections import defaultdict
from datetime import datetime
from typing import Literal, TypedDict
from uuid import UUID

from pydantic import BaseModel, Field

from sca.core.models import TERMINAL_STATES, TaskState
from sca.core.task_dag import DAGEdge, EdgeKind
from sca.eval.trajectory import is_errored, overall_passed

Status = Literal["queued", "running", "succeeded", "failed", "stopped"]
Kind = Literal["evolution", "generation", "candidate", "eval_run", "case_task", "promotion", "champion"]
ACTIVE = {"queued", "running"}
RUN_STATUS: dict[str, Status] = {
    "queued": "queued", "running": "running", "awaiting_approval": "running",
    "completed": "succeeded", "succeeded": "succeeded", "failed": "failed", "stopped": "stopped",
    "budget_exhausted": "stopped", "patience_exhausted": "stopped", "cancelled": "stopped",
}
CASE_STATUS: dict[str, Status] = {
    TaskState.QUEUED: "queued", TaskState.PAUSED: "queued", TaskState.SUCCEEDED: "succeeded",
    TaskState.FAILED: "failed", TaskState.CANCELLED: "stopped",
}
SPLITS = {"dev": "开发集", "holdout": "留出集"}


class EvolutionHistory(TypedDict):
    evolution: dict
    candidates: list[dict]
    runs: list[dict]
    results: list[dict]
    tasks: list[dict]
    champions: list[dict]


class EvolutionNode(BaseModel):
    id: str
    kind: Kind
    label: str
    status: Status
    score: float | None = None
    cost_usd: float | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float | None = None
    tokens: int | None = None
    task_id: UUID | None = None
    meta: dict = Field(default_factory=dict)


class EvolutionDAG(BaseModel):
    nodes: list[EvolutionNode]
    edges: list[DAGEdge]

    def add(self, node: EvolutionNode, parents: list[EvolutionNode], kind: EdgeKind) -> None:
        self.nodes.append(node)
        self.edges.extend(DAGEdge(source=parent.id, target=node.id, kind=kind) for parent in parents)


def _ordered(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: (row["created_at"], row["id"]))


def _overlay(run: dict) -> dict:
    return (run["variant"].get("variant") or {}).get("agent_overlay") or {}


def _associate_runs(
    history: EvolutionHistory, groups: dict[int, list[dict]], promoted: dict[UUID, dict],
) -> tuple[dict | None, dict[UUID, dict[str, dict]], dict[int, list[tuple[dict, str]]]]:
    runs = {row["id"]: row for row in _ordered(history["runs"])}
    owned: dict[UUID, dict[str, dict]] = defaultdict(dict)
    claimed: set[UUID] = set()
    candidates = _ordered(history["candidates"])
    for candidate in candidates:
        for split in SPLITS:
            run_id = candidate.get(f"{split}_run_id")
            if run_id in runs and run_id not in claimed:
                owned[candidate["id"]][split] = runs[run_id]
                claimed.add(run_id)

    baseline = next((run for run in runs.values() if run["id"] not in claimed
                     and run["variant"].get("split") == "dev"
                     and (not candidates or run["created_at"] <= candidates[0]["created_at"])), None)
    incumbent = _overlay(baseline) if baseline else None
    incumbents = {}
    for generation, group in groups.items():
        incumbents[generation] = incumbent
        winner = next((c for c in group if c["decision"] == "promoted" or c["id"] in promoted), None)
        if winner:
            incumbent = winner["overlay"]

    extras: dict[int, list[tuple[dict, str]]] = defaultdict(list)
    candidate_times = [c["created_at"] for c in candidates]
    for run in runs.values():
        if run["id"] in claimed or run is baseline:
            continue
        split = run["variant"].get("split")
        index = bisect_right(candidate_times, run["created_at"]) - 1
        if index < 0:
            extras[0].append((run, "unassigned"))
            continue
        candidate = candidates[index]
        generation = candidate["generation"]
        overlay = _overlay(run)
        if split == "holdout" and overlay == incumbents[generation]:
            extras[generation].append((run, "comparison"))
            continue
        # The workflow records the candidate before dev and saves the eval IDs after each split.
        # Until then, use the current generation, ordering and the stored overlay.
        possible = [candidate] if split == "dev" else groups[generation] if split == "holdout" else []
        matches = [c for c in possible if c["created_at"] <= run["created_at"]
                   and c["decision"] != "invalid" and c["overlay"] == overlay
                   and split not in owned[c["id"]] and not c.get(f"{split}_run_id")]
        if len(matches) == 1:
            owned[matches[0]["id"]][split] = run
        else:
            extras[generation].append((run, "unassigned"))
    return baseline, owned, extras


def _live_case(run_id: UUID, task: dict) -> tuple[str | None, int | None]:
    prefix = f"eval:{run_id}:"
    key = task.get("idempotency_key") or ""
    if key.startswith(prefix):
        name, _, repeat = key[len(prefix):].rpartition(":")
        if name and repeat.isascii() and repeat.isdecimal() and len(repeat) <= 3:
            return name, int(repeat)
    return None, None


def _case_node(run_id: UUID, task: dict, result: dict | None = None) -> EvolutionNode:
    task_id, state = task["id"], task["state"]
    status = CASE_STATUS.get(state, "running")
    score = None
    cost = task.get("cost_micro_usd")
    cost = cost / 1e6 if isinstance(cost, (float, int)) else None
    if result:
        name, repeat = result["case_id"], result["repeat"]
        cost = float(result["cost_usd"])
        if not is_errored(result):
            score = float(overall_passed(result))
            if status == "succeeded" and score == 0:
                status = "failed"
    else:
        name, repeat = _live_case(run_id, task)
    label = f"{name} · 第 {repeat + 1} 次" if name is not None else f"用例 · {str(task_id)[:8]}"
    return EvolutionNode(
        id=f"case:{run_id}:{task_id}", kind="case_task", label=label, status=status, score=score,
        cost_usd=cost, task_id=task_id, started_at=task["created_at"],
        finished_at=task["updated_at"] if state in TERMINAL_STATES else None,
        duration_s=result["duration_s"] if result else None, tokens=result["tokens"] if result else None,
        meta={"parent": f"eval:{run_id}", "case_id": name, "repeat": repeat, "state": state},
    )


def _cases(history: EvolutionHistory) -> dict[UUID, list[EvolutionNode]]:
    tasks = {row["id"]: row for row in history["tasks"]}
    run_ids = {str(run["id"]): run["id"] for run in history["runs"]}
    cases: dict[UUID, list[EvolutionNode]] = defaultdict(list)
    recorded: set[tuple[UUID, UUID]] = set()
    for row in history["results"]:
        task = tasks.get(row["task_id"])
        key = (row["run_id"], row["task_id"])
        if task is None or key in recorded:
            continue
        recorded.add(key)
        cases[row["run_id"]].append(_case_node(row["run_id"], task, row))
    collected = {task_id for _, task_id in recorded}
    for task in _ordered(history["tasks"]):
        run_id = run_ids.get(task.get("eval_run_id"))
        if run_id is not None and task["id"] not in collected:
            cases[run_id].append(_case_node(run_id, task))
    return cases


def _cost(nodes: list[EvolutionNode]) -> float | None:
    known = [node.cost_usd for node in nodes if node.cost_usd is not None]
    return sum(known) if known else None


def _add_run(
    dag: EvolutionDAG, run: dict, cases: list[EvolutionNode], parents: list[EvolutionNode],
    *, split: str | None = None, role: str = "candidate",
) -> tuple[EvolutionNode, list[EvolutionNode]]:
    split = split or run["variant"].get("split")
    title = {"baseline": "基线评测", "comparison": "当代冠军评测", "unassigned": "未关联评测"}.get(role, "评测")
    label = f"{title} · {SPLITS[split]}" if split in SPLITS else title
    scores = [case.score for case in cases if case.score is not None]
    node = EvolutionNode(
        id=f"eval:{run['id']}", kind="eval_run", label=label, status=RUN_STATUS[run["status"]],
        score=sum(scores) / len(scores) if scores else None, cost_usd=_cost(cases),
        started_at=run["created_at"], finished_at=run["finished_at"],
        meta={"run_id": str(run["id"]), "split": split, "role": role,
              "state": run["status"], "reason": run["variant"].get("reason")},
    )
    dag.add(node, parents, "sequence")
    for case in cases:
        dag.add(case, [node], "spawn")
    return node, cases or [node]


def _candidate_status(decision: str, runs: dict[str, dict], evolution_status: Status) -> Status:
    if decision == "promoted":
        return "succeeded"
    if decision in {"invalid", "rejected"}:
        return "failed"
    statuses = {RUN_STATUS[run["status"]] for run in runs.values()}
    if evolution_status not in ACTIVE:
        return "failed" if evolution_status == "failed" else "stopped"
    if "running" in statuses:
        return "running"
    if "failed" in statuses:
        return "failed"
    if "stopped" in statuses:
        return "stopped"
    return "running" if "succeeded" in statuses else "queued"


def _add_candidate(
    dag: EvolutionDAG, generation: EvolutionNode, number: int, row: dict, runs: dict[str, dict],
    cases: dict[UUID, list[EvolutionNode]], *, champion: dict | None, evolution_status: Status,
) -> tuple[EvolutionNode, list[EvolutionNode]]:
    decision = "promoted" if champion else row["decision"]
    node = EvolutionNode(
        id=f"candidate:{row['id']}", kind="candidate", label=f"候选 {number} · {row['hypothesis']}",
        status=_candidate_status(decision, runs, evolution_status), started_at=row["created_at"],
        meta={"hypothesis": row["hypothesis"], "decision": decision, "reason": row.get("reason")},
    )
    dag.add(node, [generation], "spawn")
    costs = []
    tail = [node]
    for split in SPLITS:
        metrics = row.get(f"{split}_metrics") or {}
        evaluation = None
        if run := runs.get(split):
            evaluation, tail = _add_run(dag, run, cases.get(run["id"], []), tail, split=split)
        if split == "dev":
            node.score = metrics.get("pass_rate")
            if node.score is None and evaluation is not None:
                node.score = evaluation.score
        cost = evaluation.cost_usd if evaluation is not None else None
        if cost is None:
            cost = metrics.get("cost_usd")
        if cost is not None:
            costs.append(cost)
    node.cost_usd = sum(costs) if costs else None
    return node, tail


def _promotion(
    generation: EvolutionNode, number: int, branches: list[EvolutionNode],
    winner: EvolutionNode | None, status: Status,
) -> EvolutionNode:
    decision, title, state = "rejected", "未晋升", "failed"
    if winner:
        decision, title, state = "promoted", "已晋升", "succeeded"
    elif status in ACTIVE and any(node.meta["decision"] == "pending" for node in branches):
        decision, title, state = "pending", "待定", "queued"
        if any(node.meta["reason"] == "awaiting approval" for node in branches):
            title, state = "待审批", "running"
    elif generation.status == "stopped":
        state = "stopped"
    return EvolutionNode(
        id=f"promotion:{number}", kind="promotion", label=f"晋升决定 · {title}", status=state,
        score=winner.score if winner else None,
        meta={"decision": decision, "reason": winner.meta["reason"] if winner else None},
    )


def build_evolution_dag(history: EvolutionHistory) -> EvolutionDAG:
    row = history["evolution"]
    status = RUN_STATUS[row["status"]]
    root = EvolutionNode(
        id=f"evolution:{row['id']}", kind="evolution", label=f"演进 · {str(row['id'])[:8]}", status=status,
        cost_usd=float(row["spent_usd"]), started_at=row["created_at"], finished_at=row["finished_at"],
        meta={"state": row["status"], "reason": row["config"].get("reason"), "model": row["config"].get("model")},
    )
    dag = EvolutionDAG(nodes=[root], edges=[])
    groups: dict[int, list[dict]] = defaultdict(list)
    for candidate in sorted(_ordered(history["candidates"]), key=lambda c: c["generation"]):
        groups[candidate["generation"]].append(candidate)
    promoted = {row["candidate_id"]: row for row in history["champions"]}
    cases = _cases(history)
    baseline, owned, extras = _associate_runs(history, groups, promoted)
    tail = [root]
    if baseline:
        initial, tail = _add_run(dag, baseline, cases.get(baseline["id"], []), tail, role="baseline")
        root.score = initial.score
    for run, role in extras[0]:
        _add_run(dag, run, cases.get(run["id"], []), [root], role=role)

    for number, group in groups.items():
        generation = EvolutionNode(id=f"generation:{number}", kind="generation", label=f"第 {number} 代",
                                   status="queued", started_at=group[0]["created_at"])
        dag.add(generation, tail, "sequence")
        branches, feeders = [], []
        winner = None
        champion = None
        for index, candidate in enumerate(group, start=1):
            registered = promoted.get(candidate["id"])
            node, ends = _add_candidate(dag, generation, index, candidate, owned[candidate["id"]], cases,
                                        champion=registered, evolution_status=status)
            branches.append(node)
            feeders.extend(ends)
            if node.meta["decision"] == "promoted":
                winner, champion = node, registered
        evaluations = []
        for run, role in extras[number]:
            evaluation, ends = _add_run(dag, run, cases.get(run["id"], []), [generation], role=role)
            evaluations.append(evaluation)
            feeders.extend(ends)
        generation.cost_usd = _cost(branches + evaluations)
        generation.score = max((node.score for node in branches if node.score is not None), default=None)

        pending = any(node.meta["decision"] == "pending" for node in branches)
        if pending and status in ACTIVE:
            generation.status = "running"
        elif pending or any(run.status == "failed" for run in evaluations):
            generation.status = "failed" if status == "failed" else "stopped"
        else:
            generation.status = "succeeded"
        decision = _promotion(generation, number, branches, winner, status)
        dag.add(decision, feeders, "merge")
        tail = [decision]
        if champion:
            dev = champion["metrics"].get("dev") or {}
            champion_node = EvolutionNode(
                id=f"champion:{champion['id']}", kind="champion", label=f"冠军 · {str(champion['id'])[:8]}",
                status="succeeded", score=dev.get("pass_rate"), cost_usd=dev.get("cost_usd"),
                started_at=champion["created_at"], meta={"champion_id": str(champion["id"])},
            )
            dag.add(champion_node, tail, "select")
            tail = [champion_node]
            if champion["id"] == row.get("best_champion_id"):
                root.score = champion_node.score
    return dag
