"""Read timings and usage from task events, including events written by older workers."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any

from sca.core.models import Event

type Scope = tuple[str, int | None]
MAIN: Scope = ("", None)
OPERATION_EVENTS = frozenset({"agent.command", "agent.tool", "agent.file_change"})
STARTED = ("started", "pending", "running", "in_progress")

PAIRS = {
    "admission.granted": "admission.waiting",
    "plan.completed": "plan.started",
    "plan.rejected": "plan.started",
    "subtask.started": "subtask.queued",
    "subtask.completed": "subtask.started",
    "agent.turn_completed": "agent.turn_started",
    "agent.turn_interrupted": "agent.turn_started",
    "verification.completed": "verification.started",
    "acceptance.completed": "acceptance.started",
    "ci.completed": "ci.requested",
    "review.submitted": "review.requested",
}
TIMING_CONTEXT = frozenset(PAIRS.values()) | OPERATION_EVENTS | {"agent.activity", "agent.usage", "agent.error"}


def seconds(start: datetime | None, end: datetime) -> float | None:
    return max(0.0, (end - start).total_seconds()) if start is not None else None


def number(value: Any) -> float | int | None:
    if type(value) in (int, float) and 0 <= value <= 2**63 - 1 and isfinite(value):
        return value
    return None


def duration(payload: dict) -> float | None:
    if (value := number(payload.get("duration_s"))) is not None:
        return float(value)
    if (value := number(payload.get("duration_ms"))) is not None:
        return value / 1000
    return None


def event_scope(payload: dict) -> Scope:
    name = payload.get("subtask")
    candidate = payload.get("best_of_attempt", payload.get("best_of_candidate"))
    return name if isinstance(name, str) else "", candidate if type(candidate) is int else None


def tool_activity(type_: str, payload: dict) -> tuple[str, object]:
    if type_ == "agent.activity":
        operation = payload.get("operation")
        return operation if isinstance(operation, str) else "agent", payload.get("detail")
    inputs, name = payload.get("input"), payload.get("name")
    args = inputs if isinstance(inputs, dict) else {}
    if type_ == "agent.command" or name in ("bash", "pwsh", "shell"):
        return "command", payload.get("command") or args.get("command")
    if type_ == "agent.file_change" or name in ("edit", "write", "patch", "apply_patch"):
        path = args.get("file_path") or args.get("path") or args.get("filePath")
        changes = payload.get("changes")
        if not path and isinstance(changes, list) and changes:
            first = changes[0]
            path = first.get("path") if isinstance(first, dict) else first
        return "edit", path
    return "tool", name


def event_is_start(event: Event) -> bool:
    return event.type == "agent.activity" and event.payload.get("status") == "started" or (
        event.type in OPERATION_EVENTS and event.payload.get("status") in STARTED
    )


def tool_key(event: Event) -> str:
    call_id = event.payload.get("call_id")
    if isinstance(call_id, str):
        return call_id
    operation, _ = tool_activity(event.type, event.payload)
    return operation


@dataclass
class Charge:
    tokens: int = 0
    native_cost: float | None = None
    settled_cost: float | None = None

    @property
    def cost(self) -> float | None:
        return self.settled_cost if self.settled_cost is not None else self.native_cost

    def read(self, event: Event) -> None:
        p = event.payload
        if event.type == "agent.usage":
            self.tokens += int(number(p.get("tokens")) or (
                (number(p.get("input_tokens")) or 0) + (number(p.get("output_tokens")) or 0)
            ))
        cost = number(p.get("cost_usd", p.get("cost")))
        if cost is None:
            return
        if event.type == "agent.turn_completed" or p.get("source") == "model_gateway":
            self.settled_cost = cost
        else:
            self.native_cost = (self.native_cost or 0) + cost


class TaskTimings:
    def __init__(self, events: list[Event]) -> None:
        self.durations: dict[int, float] = {}
        self.costs: dict[int, float] = {}
        self.charges: dict[Scope, dict[tuple[int, int], Charge]] = defaultdict(dict)
        self.turn_windows: dict[tuple[Scope, int, int], tuple[datetime, datetime | None]] = {}
        self._attempts: dict[tuple[Scope, int], int] = {}
        self._starts: dict[tuple[Scope, str, int], datetime] = {}
        self._calls: dict[tuple[Scope, int, str], datetime] = {}
        self._setup: dict[Scope, datetime] = {}
        for event in events:
            self.read(event)

    def totals(self, scope: Scope) -> tuple[int, float | None]:
        charges = self.charges.get(scope, {}).values()
        costs = [cost for charge in charges if (cost := charge.cost) is not None]
        return sum(charge.tokens for charge in charges), sum(costs) if costs else None

    def read(self, event: Event) -> None:
        p, type_, at = event.payload, event.type, event.created_at
        scope = event_scope(p)
        turn = p.get("attempt" if type_.startswith("plan.") else "turn")
        turn = turn if type(turn) is int else int(type_.startswith("plan."))
        key = (scope, turn)
        elapsed = duration(p)
        if type_ in PAIRS.values():
            self._starts[scope, type_, turn] = at
        if type_ == "agent.turn_started":
            self._attempts[key] = self._attempts.get(key, 0) + 1
            self._calls = {k: value for k, value in self._calls.items() if k[:2] != key}
            prior = (*key, self._attempts[key] - 1)
            if prior in self.turn_windows and self.turn_windows[prior][1] is None:
                self.turn_windows[prior] = (self.turn_windows[prior][0], at)
            self.turn_windows[(*key, self._attempts[key])] = (at, None)
        if (type_ in ("agent.turn_completed", "agent.turn_interrupted")
                or type_ == "agent.error" and p.get("attempt") is not None):
            window = (*key, self._attempts.get(key, 0))
            if window in self.turn_windows and self.turn_windows[window][1] is None:
                self.turn_windows[window] = (self.turn_windows[window][0], at)
        if type_ in PAIRS:
            start = self._starts.get((scope, PAIRS[type_], turn))
            elapsed = elapsed if elapsed is not None else seconds(start, at)
        if type_ == "admission.granted":
            measured = number(p.get("wait_seconds"))
            elapsed = measured if measured is not None else elapsed
        if type_ == "verification.completed" and p.get("skipped"):
            elapsed = None
        if type_ in OPERATION_EVENTS or type_ == "agent.activity":
            call = (*key, tool_key(event))
            if event_is_start(event):
                self._calls.setdefault(call, at)
            elif type_ in OPERATION_EVENTS:
                start = self._calls.pop(call, None)
                elapsed = elapsed if elapsed is not None else seconds(start, at)
        if type_ == "task.state_changed" and p.get("to") == "PROVISIONING":
            self._setup[MAIN] = at
        elif type_ == "subtask.started":
            self._setup[scope] = at
        elif type_ in ("workspace.prepared", "sandbox.created", "sandbox.restored", "setup.completed"):
            previous = self._setup.get(scope)
            if (elapsed is None and type_ != "sandbox.restored"
                    and not p.get("skipped") and p.get("phase") != "after_setup"):
                elapsed = seconds(previous, at)
            self._setup[scope] = at
        if elapsed is not None:
            self.durations[event.seq] = elapsed

        if type_ in ("agent.usage", "agent.turn_completed"):
            charge = self.charges[scope].setdefault((turn, self._attempts.get(key, 0)), Charge())
            charge.read(event)
            if type_ == "agent.turn_completed" and charge.cost is not None:
                self.costs[event.seq] = charge.cost
        elif type_ == "subtask.completed":
            costs = [cost for member in self.charges if member[0] == scope[0]
                     and (cost := self.totals(member)[1]) is not None]
            if costs:
                self.costs[event.seq] = sum(costs)

    def agent_seconds(self, scope: Scope, end: datetime) -> float | None:
        times = [seconds(start, finish or end) or 0
                 for (lane, _, _), (start, finish) in self.turn_windows.items() if lane == scope]
        return sum(times) if times else None
