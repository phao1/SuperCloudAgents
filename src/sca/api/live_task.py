"""Project the task's current phase and Agent lanes from event and worker timings."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sca.api.live_status import (
    LiveAgent,
    LivePhaseDuration,
    LiveSnapshot,
    LiveStep,
    LiveSummary,
    latest,
    live_text,
    stalled,
)
from sca.core.live_timing import (
    MAIN,
    OPERATION_EVENTS,
    Scope,
    TaskTimings,
    duration,
    event_is_start,
    event_scope,
    number,
    seconds,
    tool_activity,
    tool_key,
)
from sca.core.models import TERMINAL_STATES, Event, Task, TaskState
from sca.live_heartbeat import WorkerBeat

TASK_PHASES = {
    TaskState.QUEUED: "queue",
    TaskState.PROVISIONING: "setup",
    TaskState.RUNNING: "agent",
    TaskState.VERIFYING: "verification",
    TaskState.AWAITING_REVIEW: "review",
    TaskState.PAUSED: "paused",
}
RUNNING_TASK_STATES = {TaskState.PROVISIONING, TaskState.RUNNING, TaskState.VERIFYING}
PREPARATION_EVENTS = {"workspace.prepared", "sandbox.created", "sandbox.restored", "setup.completed"}
SUBTASK_EVENTS = {"subtasks.scheduled", "subtask.queued", "subtask.started", "subtask.completed"}
VERIFICATION_STARTS = {"verification.started", "acceptance.started", "ci.requested"}
VERIFICATION_ENDS = {"verification.completed", "acceptance.completed", "ci.completed"}
LANE_EVENTS = OPERATION_EVENTS | PREPARATION_EVENTS | VERIFICATION_STARTS | VERIFICATION_ENDS | {
    "subtask.queued", "subtask.started", "subtask.completed", "best_of.attempt_completed",
    "agent.turn_started", "agent.turn_completed", "agent.turn_interrupted", "agent.activity",
    "agent.usage", "agent.message", "agent.error", "model.provider_error", "workspace.checkpointed",
}
LIVE_TASK_EVENTS = frozenset(LANE_EVENTS | SUBTASK_EVENTS | {
    "task.state_changed", "admission.waiting", "admission.granted",
    "plan.started", "plan.completed", "plan.rejected", "best_of.started", "subtasks.merged", "output.written",
})

ROOT_WORK = {
    "plan": "plan", "workspace": "setup", "sandbox": "setup", "setup": "setup",
    "verification": "verification", "merge": "merge", "output": "output",
}


def _step(operation: str, at: datetime, detail: object = None) -> LiveStep:
    return LiveStep(operation=operation, started_at=at, detail=live_text(detail))


def _start(event: Event) -> datetime:
    return event.created_at - timedelta(seconds=min(duration(event.payload) or 0, 365 * 86400))


def _is_planner(scope: Scope) -> bool:
    name, candidate = scope
    return candidate is None and name.startswith("plan-") and name.removeprefix("plan-").isdigit()


@dataclass
class LaneTrace:
    started_at: datetime | None = None
    finished_at: datetime | None = None
    event_at: datetime | None = None
    backend: str | None = None
    turn: int | None = None
    current: LiveStep | None = None
    calls: dict[str, LiveStep] = field(default_factory=dict)
    provider_problem: str | None = None

    def read(self, event: Event, default_backend: str | None) -> None:
        type_, p, at = event.type, event.payload, event.created_at
        self.event_at = at
        self.backend = live_text(p.get("backend"), 60) or self.backend or default_backend
        if type_ in ("subtask.completed", "best_of.attempt_completed"):
            self.finished_at = at
            self.calls.clear()
            return
        if self.started_at is None and type_ != "subtask.queued":
            self.started_at = _start(event)
        if type_ == "subtask.queued":
            self.current = _step("slot", at)
        elif type_ == "subtask.started":
            self.current = _step("workspace", at)
        elif type_ == "agent.turn_started":
            self.turn = p.get("turn") if type(p.get("turn")) is int else 1
            self.calls.clear()
            self.provider_problem = None
            self.current = _step("agent" if self.backend == "fake" else "model", at)
        elif event_is_start(event):
            operation, detail = tool_activity(type_, p)
            if operation in ("command", "edit", "tool"):
                self.calls.setdefault(tool_key(event), _step(operation, at, detail))
            elif operation in ("model", "model_retry", "model_rate_limit", "settle"):
                if operation == "model":
                    self.provider_problem = None
                if operation == "model_retry" and self.provider_problem == "rate_limit":
                    operation = "model_rate_limit"
                if operation in ("model_retry", "model_rate_limit") and type(p.get("retry_attempt")) is int:
                    detail = f"第 {p['retry_attempt']} 次尝试"
                self.current = _step(operation, at, detail)
        elif type_ in OPERATION_EVENTS:
            self.calls.pop(tool_key(event), None)
            self.current = _step("agent", at)
        elif type_ == "model.provider_error" or type_ == "agent.error":
            provider = p.get("model_provider_error")
            problem = provider.get("kind") if isinstance(provider, dict) else p.get("kind")
            if problem in ("rate_limit", "unavailable"):
                self.provider_problem = problem
            if self.provider_problem:
                retry = "model_rate_limit" if self.provider_problem == "rate_limit" else "model_retry"
                if self.current is None or self.current.operation != retry:
                    self.current = _step(retry, at)
                self.calls.clear()
        elif type_ in ("agent.turn_completed", "agent.turn_interrupted"):
            self.calls.clear()
            self.provider_problem = None
            self.current = _step("checkpoint" if type_ == "agent.turn_completed" else "agent", at)
        elif type_ == "agent.activity" and p.get("operation") == "settle":
            if self.provider_problem is None:
                self.current = _step("checkpoint", at)
        elif type_ in VERIFICATION_STARTS:
            self.current = _step("ci" if type_ == "ci.requested" else "verification", at)
        elif type_ in PREPARATION_EVENTS | VERIFICATION_ENDS | {"workspace.checkpointed"}:
            self.current = _step("agent", at)

    def activity(self, beats: list[WorkerBeat]) -> LiveStep | None:
        current = max(self.calls.values(), key=lambda step: step.started_at, default=self.current)
        notes = [beat for beat in beats if beat.operation not in ("turn", "plan")
                 and (current is None or beat.started_at >= current.started_at)]
        if notes:
            beat = max(notes, key=lambda step: step.started_at)
            return _step(beat.operation, beat.started_at)
        return current


class TaskTrace:
    def __init__(self, task: Task, events: list[Event], default_backend: str) -> None:
        self.task = task
        self.default_backend = live_text(task.spec.get("agent_backend"), 60) or default_backend
        self.lanes: dict[Scope, LaneTrace] = defaultdict(LaneTrace)
        self.subtasks: dict[str, dict] = {}
        self.scheduled_at = task.created_at
        self.phase = _step("queue", task.created_at)
        self.phase_times: dict[str, float] = {}
        self.planning = False
        self.plan_ended_at: datetime | None = None
        self.in_admission = False
        self.output_finished_at: datetime | None = None
        self.state_at = task.created_at
        self.finished_at: datetime | None = None
        self.timing = TaskTimings(events)
        self._declare(task.spec.get("subtasks"))
        for event in events:
            self._read(event)

    def _declare(self, rows: object) -> None:
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(name := row.get("name"), str):
                self.subtasks.setdefault(name, {}).update(row)

    def _advance(self, phase: str, at: datetime, detail: object = None) -> None:
        if phase == self.phase.operation:
            if detail is not None:
                self.phase.detail = live_text(detail)
            return
        at = max(at, self.phase.started_at)
        self.phase_times[self.phase.operation] = self.phase_times.get(self.phase.operation, 0) + (
            seconds(self.phase.started_at, at) or 0
        )
        self.phase = _step(phase, at, detail)

    def _read(self, event: Event) -> None:
        p, at, type_ = event.payload, event.created_at, event.type
        scope = event_scope(p)
        if type_ == "best_of.attempt_completed" and type(p.get("attempt")) is int:
            scope = (scope[0], p["attempt"])
        if type_ in LANE_EVENTS:
            self.lanes[scope].read(event, self.default_backend)
            if (name := scope[0]) and type(p.get("agent_lane")) is int and not _is_planner(scope):
                self.subtasks.setdefault(name, {}).update(agent_lane=p["agent_lane"], agent_label=p.get("agent_label"))
        if type_ == "task.state_changed":
            self.state_at = at
            state = p.get("to")
            if not isinstance(state, str):
                return
            if state in TERMINAL_STATES:
                self.finished_at = max(at, self.output_finished_at or at)
                self._advance("finished", self.finished_at)
            elif state in TASK_PHASES:
                self._advance(TASK_PHASES[state], at)
            self.in_admission = False
        elif type_ in ("admission.waiting", "admission.granted"):
            self.in_admission = type_ == "admission.waiting"
            self._advance("queue", at, (p.get("reason") or "slot") if self.in_admission else None)
        elif type_ == "plan.started":
            self.planning = True
            self._advance("plan", at)
        elif type_ in ("plan.completed", "plan.rejected"):
            self.planning, self.plan_ended_at = False, at
            self._declare(p.get("subtasks"))
        elif type_ in SUBTASK_EVENTS or type_ == "best_of.started":
            if type_ == "subtasks.scheduled":
                self.scheduled_at = at
                self._declare(p.get("subtasks"))
            self._advance("subtasks", at)
        elif type_ == "agent.turn_started" and not self.planning and not _is_planner(scope):
            self._advance("agent" if scope == MAIN else "subtasks", at)
        elif scope == MAIN:
            if type_ in PREPARATION_EVENTS and not self.planning:
                self._advance("setup", _start(event))
            elif type_ in VERIFICATION_STARTS | VERIFICATION_ENDS:
                self._advance("verification", _start(event))
            elif type_ == "subtasks.merged":
                self._advance("merge", _start(event))
            elif type_ == "output.written":
                self.output_finished_at = at
                self._advance("output", _start(event))

    def _identity(self, scope: Scope, lane: LaneTrace) -> LiveAgent:
        name, candidate = scope
        sub = self.subtasks.get(name, {})
        agent_lane = sub.get("agent_lane")
        label = f"Agent {agent_lane}" if type(agent_lane) is int else live_text(sub.get("agent_label"), 60)
        if candidate is not None:
            label = f"{label} · 候选 {candidate}" if label else f"候选 {candidate}"
        return LiveAgent(label=label or "Agent 1", backend=lane.backend or self.default_backend,
                         subtask=live_text(name, 80) if not _is_planner(scope) else None, turn=lane.turn)

    def _pending(self, name: str, lane: LaneTrace) -> LiveStep:
        if lane.current is not None:
            return lane.current
        at = self.scheduled_at
        steps = self.subtasks[name].get("depends_on")
        prerequisites = [need for need in steps if isinstance(need, str)] if isinstance(steps, list) else []
        waiting = [need for need in prerequisites if self.lanes[need, None].finished_at is None]
        if waiting:
            return _step("dependency", at, "、".join(waiting))
        at = latest(at, *(self.lanes[need, None].finished_at for need in prerequisites)) or at
        return _step("slot", at)

    def _agent(self, scope: Scope, lane: LaneTrace, beats: list[WorkerBeat], now: datetime,
               threshold: int) -> LiveAgent | None:
        activity = lane.activity(beats)
        if activity is None or lane.finished_at is not None:
            return None
        last = latest(lane.event_at, activity.started_at, *(beat.at for beat in beats)) or activity.started_at
        agent = self._identity(scope, lane)
        agent.activity, agent.last_activity_at = activity, last
        agent.stalled = stalled(activity.operation not in ("slot", "dependency", "ci"), last, now, threshold)
        series = self.subtasks.get(scope[0], {}).get("agent_lane")
        scopes = ([(name, None) for name, row in self.subtasks.items() if row.get("agent_lane") == series]
                  if type(series) is int and scope[1] is None else [scope])
        totals = [self.timing.totals(member) for member in scopes]
        agent.tokens = sum(tokens for tokens, _ in totals)
        costs = [cost for _, cost in totals if cost is not None]
        agent.cost_usd = sum(costs) if costs else None
        agent.duration_s = seconds(lane.started_at or activity.started_at, now)
        return agent

    def _current_agents(
        self, phase: LiveStep, beats: list[WorkerBeat], now: datetime, threshold: int,
    ) -> list[LiveAgent]:
        by_scope: dict[Scope, list[WorkerBeat]] = defaultdict(list)
        for beat in beats:
            by_scope[beat.scope].append(beat)
        agents = []
        occupied: set[int] = set()
        has_planner = any(_is_planner(scope) for scope in self.lanes.keys() | by_scope.keys())
        candidates = {scope[0] for scope in self.lanes.keys() | by_scope.keys() if scope[1] is not None
                      and self.lanes[scope].finished_at is None}
        for scope in dict.fromkeys([*self.lanes, *by_scope]):
            name, candidate = scope
            lane = self.lanes[scope]
            if phase.operation == "plan" and has_planner and scope == MAIN:
                continue
            if scope == MAIN and phase.operation not in ("agent", "plan") and not any(
                beat.operation in ("turn", "settle") for beat in by_scope[scope]
            ):
                continue
            if _is_planner(scope) and (phase.operation != "plan" or self.plan_ended_at
                                       and lane.event_at and lane.event_at <= self.plan_ended_at):
                continue
            if (name or candidate is not None) and not _is_planner(scope) and phase.operation != "subtasks":
                continue
            if name in candidates and candidate is None:
                continue
            if (lane.started_at is None and phase.operation == "subtasks"
                    and name in self.subtasks and not by_scope[scope]):
                continue
            if lane.current is None and phase.operation in ("agent", "plan"):
                turn = next((beat for beat in by_scope[scope] if beat.operation == "turn"), None)
                if turn is None and scope != MAIN:
                    continue
                lane.current = _step("agent" if (lane.backend or self.default_backend) == "fake" else "model",
                                     turn.started_at if turn else phase.started_at)
            if agent := self._agent(scope, lane, by_scope[scope], now, threshold):
                agents.append(agent)
                lane_number = self.subtasks.get(name, {}).get("agent_lane")
                if type(lane_number) is int:
                    occupied.add(lane_number)
        if phase.operation == "subtasks":
            for name, spec in self.subtasks.items():
                scope = (name, None)
                lane = self.lanes[scope]
                number_ = spec.get("agent_lane")
                if name in candidates or lane.finished_at is not None or lane.started_at is not None:
                    continue
                if type(number_) is int and number_ in occupied:
                    continue
                agent = self._identity(scope, lane)
                agent.activity = self._pending(name, lane)
                agents.append(agent)
                if type(number_) is int:
                    occupied.add(number_)
        return agents

    def _agent_summary(self, end: datetime) -> list[LiveAgent]:
        rows: dict[tuple[str, object], LiveAgent] = {}
        for scope, lane in self.lanes.items():
            tokens, cost = self.timing.totals(scope)
            if lane.turn is None and not (tokens or cost is not None):
                continue
            agent = self._identity(scope, lane)
            series = self.subtasks.get(scope[0], {}).get("agent_lane")
            key = ("lane", series) if type(series) is int and scope[1] is None else ("scope", scope)
            if key not in rows:
                rows[key] = LiveAgent(label=agent.label, backend=agent.backend,
                                      subtask=None if key[0] == "lane" else agent.subtask)
            row = rows[key]
            if scope == MAIN or _is_planner(scope):
                elapsed = self.timing.agent_seconds(scope, end)
            else:
                elapsed = seconds(lane.started_at, lane.finished_at or lane.event_at or end)
            if elapsed is not None:
                row.duration_s = (row.duration_s or 0) + elapsed
            row.tokens += tokens
            if cost is not None:
                row.cost_usd = (row.cost_usd or 0) + cost
        if MAIN in self.lanes and any(_is_planner(scope) for scope in self.lanes):
            for (kind, scope), row in rows.items():
                if kind == "scope" and _is_planner(scope):
                    row.label = "规划 Agent"
        return list(rows.values())

    def snapshot(
        self, now: datetime, threshold: int, last_event: datetime | None, beats: list[WorkerBeat],
    ) -> LiveSnapshot:
        task = self.task
        finished = task.state in TERMINAL_STATES
        writing = task.state == TaskState.AWAITING_REVIEW and any(
            beat.operation == "output" and beat.started_at >= self.state_at for beat in beats
        )
        running = writing or task.state in RUNNING_TASK_STATES and not self.in_admission
        end = (self.finished_at or task.updated_at) if finished else now
        if finished:
            self._advance("finished", end)
        elif not self.in_admission and (not running or self.phase.operation in ("queue", "finished", "paused", "review")):
            self._advance(TASK_PHASES[task.state], self.state_at)
        # Ignore a heartbeat from a stopped phase after a resume or a lane's completion.
        beats = [beat for beat in beats if running and beat.at >= self.state_at
                 and not self.lanes[beat.scope].finished_at
                 and (not _is_planner(beat.scope) or self.plan_ended_at is None
                      or beat.started_at > self.plan_ended_at)]
        root_beats = [beat for beat in beats if beat.scope == MAIN and beat.operation in ROOT_WORK
                      and beat.started_at >= self.phase.started_at]
        root_beat = max(root_beats, key=lambda beat: beat.started_at, default=None)
        if root_beat and not (self.planning and root_beat.operation != "plan"):
            root_detail = root_beat.operation if ROOT_WORK[root_beat.operation] == "setup" else None
            self._advance(ROOT_WORK[root_beat.operation], root_beat.started_at, root_detail)
        if self.phase.operation in ("agent", "plan"):
            self.lanes.setdefault(MAIN, LaneTrace())
        agents = self._current_agents(self.phase, beats, now, threshold) if running else []
        heartbeat = latest(*(beat.at for beat in beats))
        last = latest(task.created_at, self.state_at, last_event, heartbeat) or task.created_at
        costs = [cost for scope in self.timing.charges if (cost := self.timing.totals(scope)[1]) is not None]
        micro = number(task.usage.get("cost_micro_usd"))
        cost = max(micro / 1e6 if micro is not None else 0, sum(costs))
        summary = LiveSummary(
            phases=[LivePhaseDuration(phase=phase, duration_s=elapsed) for phase, elapsed in self.phase_times.items()],
            agents=self._agent_summary(end),
        ) if finished else None
        return LiveSnapshot(
            state=task.state, running=running, finished=finished, as_of=now,
            started_at=task.created_at, finished_at=end if finished else None,
            elapsed_s=seconds(task.created_at, end) or 0, phase=self.phase,
            last_event_at=last_event, last_heartbeat_at=heartbeat, last_activity_at=last,
            stall_warn_s=threshold, stalled=stalled(running, last, now, threshold),
            agents=agents, cost_usd=cost, summary=summary,
        )
