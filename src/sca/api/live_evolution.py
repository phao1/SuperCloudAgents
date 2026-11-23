"""Project evaluation progress and worker liveness for an evolution."""

from datetime import datetime
from pathlib import Path
from uuid import UUID

from sca.api.live_status import (
    LiveAgent,
    LiveCase,
    LiveEvaluation,
    LivePhaseDuration,
    LiveSnapshot,
    LiveStep,
    LiveSummary,
    latest,
    live_text,
    stalled,
)
from sca.api.live_task import RUNNING_TASK_STATES
from sca.core.live_timing import seconds
from sca.core.models import TERMINAL_STATES
from sca.db.repo import Store
from sca.eval.trajectory import is_errored, overall_passed
from sca.evolve.dag import EvolutionHistory, EvolutionNode, build_evolution_dag
from sca.evolve.steering import ACTIVE_EVOLUTION_STATES
from sca.live_heartbeat import WorkerBeat, read_beats


def _active_cases(nodes: list[EvolutionNode], run: EvolutionNode | None) -> list[EvolutionNode]:
    if run is None or run.meta["state"] not in ("queued", "running"):
        return []
    return [node for node in nodes if node.kind == "case_task" and node.meta.get("parent") == run.id
            and node.meta["state"] not in TERMINAL_STATES and node.task_id is not None]


def _case_progress(
    case: EvolutionNode, events: dict[UUID, datetime], beats: list[WorkerBeat], now: datetime, threshold: int,
) -> LiveCase:
    last = latest(case.started_at, events.get(case.task_id), *(beat.at for beat in beats)) or now
    state = case.meta["state"]
    return LiveCase(task_id=case.task_id, label=live_text(case.label, 100) or "用例", state=state,
                    started_at=case.started_at or now, last_activity_at=last,
                    stalled=stalled(state in RUNNING_TASK_STATES, last, now, threshold))


def _evaluation_progress(
    history: EvolutionHistory, run: EvolutionNode, cases: list[LiveCase],
) -> LiveEvaluation:
    run_id = run.meta["run_id"]
    variant = next(row["variant"] for row in history["runs"] if str(row["id"]) == run_id)
    results = [row for row in history["results"] if str(row["run_id"]) == run_id]
    count = len(results)
    metadata, repeat = variant.get("case_metadata"), variant.get("repeat", 1)
    total = None
    if isinstance(metadata, dict) and metadata and type(repeat) is int and repeat > 0:
        total = max(count, len(metadata) * repeat)
    elif run.meta["state"] == "completed":
        total = count
    return LiveEvaluation(split=run.meta.get("split"), role=run.meta.get("role"), completed=count, total=total,
                          passed=sum(not is_errored(row) and overall_passed(row) for row in results), cases=cases)


def _summary(nodes: list[EvolutionNode], backend: str | None, proposer_spend: float) -> LiveSummary:
    phases: dict[str, float] = {}
    elapsed, cost, tokens = 0., 0., 0
    seen: set[UUID] = set()
    for node in nodes:
        if node.kind == "eval_run" and node.finished_at is not None:
            phase = {"dev": "evaluation_dev", "holdout": "evaluation_holdout"}.get(
                node.meta["split"], "evaluation",
            )
            phases[phase] = phases.get(phase, 0) + (seconds(node.started_at, node.finished_at) or 0)
        elif node.kind == "case_task" and node.finished_at is not None and node.task_id not in seen:
            seen.add(node.task_id)
            elapsed += (node.duration_s if node.duration_s is not None
                        else seconds(node.started_at, node.finished_at) or 0)
            cost += node.cost_usd or 0
            tokens += node.tokens or 0
    agents = [LiveAgent(label="评测 Agent", backend=backend, duration_s=elapsed,
                        cost_usd=cost, tokens=tokens)] if seen else []
    if proposer_spend:
        agents.append(LiveAgent(label="反思与提议", cost_usd=proposer_spend))
    return LiveSummary(
        phases=[LivePhaseDuration(phase=phase, duration_s=elapsed) for phase, elapsed in phases.items()],
        agents=agents,
    )


def _phase(
    state: str, changed: datetime, run: EvolutionNode | None, beat: WorkerBeat | None,
    generation: EvolutionNode | None,
) -> LiveStep:
    if state in ("queued", "paused", "awaiting_approval"):
        operation = {"queued": "queue", "paused": "paused", "awaiting_approval": "review"}[state]
        return LiveStep(operation=operation, started_at=changed)
    if state not in ACTIVE_EVOLUTION_STATES:
        return LiveStep(operation="finished", started_at=changed)
    if (beat and beat.operation in ("prepare", "proposal", "prepare_evaluation")
            and (run is None or beat.started_at >= (run.started_at or changed))):
        return LiveStep(operation=beat.operation, started_at=beat.started_at)
    if run and run.meta["state"] in ("queued", "running"):
        return LiveStep(operation="evaluation", started_at=run.started_at or changed)
    if generation and generation.started_at and (run is None or generation.started_at >= (run.finished_at or changed)):
        return LiveStep(operation="proposal", started_at=generation.started_at)
    return LiveStep(operation="gate" if run else "prepare",
                    started_at=(run.finished_at if run else None) or changed)


async def evolution_live(
    store: Store, history: EvolutionHistory, data_dir: Path, now: datetime, threshold: int,
) -> LiveSnapshot:
    nodes = build_evolution_dag(history).nodes
    row = history["evolution"]
    root = nodes[0]
    state, start = row["status"], row["created_at"]
    running, finished = state == "running", state not in ACTIVE_EVOLUTION_STATES
    changed = row.get("updated_at") or start
    end = (row.get("finished_at") or changed) if finished else now
    generation = max((node for node in nodes if node.kind == "generation"),
                     key=lambda node: node.meta["band"], default=None)
    run = max((node for node in nodes if node.kind == "eval_run"),
              key=lambda node: (node.started_at or start, node.id), default=None)
    beats = read_beats(data_dir, "evolution", row["id"]) if running else []
    beat = max(beats, key=lambda item: item.started_at, default=None)
    phase = _phase(state, changed, run, beat, generation)
    in_evaluation = phase.operation == "evaluation"
    active_cases = _active_cases(nodes, run) if in_evaluation else []
    case_events = await store.live_case_events([case.task_id for case in active_cases]) if active_cases else {}
    case_beats = {case.task_id: read_beats(data_dir, "task", case.task_id) for case in active_cases}
    cases = [_case_progress(case, case_events, case_beats[case.task_id], now, threshold)
             for case in active_cases]
    cases.sort(key=lambda case: (case.state not in RUNNING_TASK_STATES, case.started_at))
    if in_evaluation and run:
        run_beats = read_beats(data_dir, "evaluation", UUID(run.meta["run_id"]))
        if not active_cases and run_beats:
            phase.detail = max(run_beats, key=lambda item: item.started_at).operation
        beats.extend(run_beats)
        for entries in case_beats.values():
            beats.extend(entries)
    candidate = None
    if state == "awaiting_approval":
        candidate = next((node.meta["column"] for node in nodes if node.kind == "candidate"
                          and node.meta["reason"] == "awaiting approval"), None)
    elif run and run.meta.get("role") == "candidate" and phase.operation in ("evaluation", "gate"):
        candidate = run.meta.get("column")

    journal = await store.live_evolution_event(row["id"]) if running else None
    event_at = latest(changed, journal, *case_events.values(),
                      *(node.finished_at or node.started_at for node in nodes)) or changed
    heartbeat = latest(*(item.at for item in beats))
    last = latest(event_at, heartbeat) or start
    return LiveSnapshot(
        state=state, running=running, finished=finished, as_of=now, started_at=start,
        finished_at=end if finished else None, elapsed_s=seconds(start, end) or 0, phase=phase,
        last_event_at=event_at, last_heartbeat_at=heartbeat, last_activity_at=last,
        stall_warn_s=threshold, stalled=stalled(running, last, now, threshold),
        cost_usd=root.cost_usd, eval_spent_usd=root.meta["eval_spent_usd"], budget_usd=row["config"].get("budget_usd"),
        generation=generation.meta["band"] if generation else (0 if run else None),
        generation_started_at=generation.started_at if generation else None, candidate=candidate,
        evaluation=_evaluation_progress(history, run, cases) if run else None,
        summary=_summary(nodes, live_text(row["config"].get("agent_backend", "opencode"), 60),
                         root.meta["proposer_spent_usd"]) if finished else None,
    )
