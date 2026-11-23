"""Project stored task events into an execution graph."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from graphlib import TopologicalSorter
from math import isfinite
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from sca.core.models import TERMINAL_STATES, Event, Task, TaskState

Status = Literal["pending", "running", "succeeded", "failed", "cancelled", "skipped"]
NodeKind = Literal[
    "task", "plan", "subtask", "attempt", "turn", "tool_group", "verification", "fix",
    "interjection", "merge", "review", "pr", "ci", "finish",
]
EdgeKind = Literal["sequence", "dependency", "spawn", "select", "merge"]
TOOL_EVENTS = {"agent.tool", "agent.command", "agent.file_change"}
EXECUTION_STEPS = {"turn", "fix", "verification"}
TURN_EVENTS = TOOL_EVENTS | {
    "agent.turn_started", "agent.turn_completed", "agent.turn_interrupted",
    "agent.usage", "agent.error", "agent.message", "model.provider_error",
}
TASK_STATUS: dict[TaskState, Status] = {
    TaskState.QUEUED: "pending", TaskState.PAUSED: "pending",
    TaskState.SUCCEEDED: "succeeded", TaskState.FAILED: "failed", TaskState.CANCELLED: "cancelled",
}


class DAGNode(BaseModel):
    id: str
    kind: NodeKind
    label: str
    status: Status = "pending"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float | None = None
    cost_usd: float | None = None
    tokens: int | None = None
    task_id: UUID
    meta: dict[str, Any] = Field(default_factory=dict)

    def start(self, at: datetime) -> None:
        if self.status == "pending":
            self.status = "running"
            self.started_at = at

    def close(self, status: Status, at: datetime | None) -> None:
        self.status, self.finished_at = status, at
        self.duration_s = (max(0.0, (at - self.started_at).total_seconds())
                           if at is not None and self.started_at is not None else None)


class DAGEdge(BaseModel):
    source: str = Field(serialization_alias="from")
    target: str = Field(serialization_alias="to")
    kind: EdgeKind


class TaskDAG(BaseModel):
    nodes: list[DAGNode]
    edges: list[DAGEdge]


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _rows(value: Any) -> list[dict]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def linked_tasks(task: Task, events: Iterable[Event]) -> dict[UUID, str | None]:
    """Return explicit links to task rows, with the logical subtask name when recorded."""
    ids = task.result.get("child_task_ids")
    ids = ids if isinstance(ids, list) else []
    links = {child_id: None for value in ids if (child_id := _uuid(value)) is not None}
    for row in _rows(task.result.get("subtasks")):
        if child_id := _uuid(row.get("task_id")):
            links[child_id] = row.get("name")
    for event in events:
        if event.task_id != task.id or event.type not in {"task.child_started", "subtask.started"}:
            continue
        payload = event.payload
        value = payload.get("child_task_id")
        if value is None and event.type == "task.child_started":
            value = payload.get("task_id")
        if child_id := _uuid(value):
            links[child_id] = payload.get("subtask") or links.get(child_id)
    links.pop(task.id, None)
    return links


def _number(value: Any) -> float | int | None:
    if type(value) in (int, float) and isfinite(value) and value >= 0:
        return value
    return None


def _tokens(data: dict) -> int | None:
    total = _number(data.get("tokens"))
    if total is None:
        input_ = _number(data.get("input_tokens"))
        output = _number(data.get("output_tokens"))
        if input_ is not None or output is not None:
            total = (input_ or 0) + (output or 0)
    return int(total) if total is not None else None


def _status(data: dict) -> Status:
    if data.get("skipped") or data.get("status") in ("skipped", "blocked"):
        return "skipped"
    if data.get("status") == "cancelled":
        return "cancelled"
    if data.get("passed") is True or data.get("status") in ("succeeded", "merged", "unchanged"):
        return "succeeded"
    return "failed"


def _fields(data: dict, *keys: str) -> dict:
    return {key: data[key] for key in keys if key in data and data[key] is not None}


class _Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, DAGNode] = {}
        self.edges: dict[tuple[str, str, EdgeKind], DAGEdge] = {}
        self.outgoing: dict[str, set[str]] = defaultdict(set)

    def add(self, task: Task, id_: str, kind: NodeKind, label: str, **meta: Any) -> DAGNode:
        node = DAGNode(id=id_, kind=kind, label=label, task_id=task.id, meta=meta)
        self.nodes[node.id] = node
        return node

    def link(self, source: DAGNode, target: DAGNode, kind: EdgeKind = "sequence") -> None:
        # Corrupt legacy relationships must not introduce cycles.
        seen, pending = set(), [target.id]
        while pending:
            item = pending.pop()
            if item == source.id:
                return
            if item not in seen:
                seen.add(item)
                pending.extend(self.outgoing.get(item, ()))
        pair = (source.id, target.id, kind)
        self.edges[pair] = DAGEdge(source=source.id, target=target.id, kind=kind)
        self.outgoing[source.id].add(target.id)

    def advance(self, lane: _Lane, node: DAGNode) -> DAGNode:
        self.link(lane.tail, node)
        lane.tail = node
        return node


@dataclass
class _Lane:
    node: DAGNode
    tail: DAGNode = field(init=False)
    turns: dict[int, DAGNode] = field(default_factory=dict)
    tools: dict[str, DAGNode] = field(default_factory=dict)
    checks: dict[tuple[str, int], DAGNode] = field(default_factory=dict)
    attempts: dict[int, _Lane] = field(default_factory=dict)
    select: DAGNode | None = None
    needs_fix: bool = False

    def __post_init__(self) -> None:
        self.tail = self.node

    @property
    def output(self) -> DAGNode:
        return self.select if self.select is not None and self.tail is self.node else self.tail


class _TaskTrace:
    def __init__(self, graph: _Graph, task: Task, events: list[Event]) -> None:
        self.graph, self.task, self.events = graph, task, events
        self.state = task.state
        self.root = _Lane(graph.add(
            task, f"task:{task.id}", "task", f"任务 · {str(task.id)[:8]}",
            task_kind=task.kind, created_at=task.created_at.isoformat(),
        ))
        self.finish = self._node(self.root, "finish", "任务结束", "finish")
        self.plans: dict[int, _Lane] = {}
        self.subtasks: dict[str, _Lane] = {}
        self.subtask_links: dict[str, _TaskTrace] = {}
        self.messages: dict[str, DAGNode] = {}
        self.plan_gate: DAGNode | None = None
        self.merge: DAGNode | None = None
        self.review: DAGNode | None = None
        self.pr: DAGNode | None = None
        self.finished_at: datetime | None = None
        self._plan_attempt: int | None = None
        self._review_count = 0

    def _node(
        self, lane: _Lane, kind: NodeKind, label: str, suffix: str | int, **meta: Any,
    ) -> DAGNode:
        if "subtask" in lane.node.meta:
            meta.setdefault("subtask", lane.node.meta["subtask"])
        if kind in EXECUTION_STEPS:
            meta["parent"] = lane.node.id
        return self.graph.add(self.task, f"{lane.node.id}/{kind}:{suffix}", kind, label, **meta)

    def _subtask(self, name: str) -> _Lane:
        if name not in self.subtasks:
            self.subtasks[name] = _Lane(self._node(
                self.root, "subtask", name, name, subtask=name, depends_on=[],
            ))
        return self.subtasks[name]

    def _plan(self, attempt: int, at: datetime | None = None) -> _Lane:
        if attempt not in self.plans:
            node = self._node(self.root, "plan", f"规划 · 第 {attempt} 次", attempt, attempt=attempt)
            if self.review and self.review.status == "running":
                self.review.close("skipped", at)
            if self._plan_attempt is not None:
                previous = self.plans[self._plan_attempt]
                if self.root.tail is previous.node:
                    self.root.tail = previous.tail
                if previous.node.status == "running":
                    previous.node.close("cancelled", at)
            self.plans[attempt] = _Lane(self.graph.advance(self.root, node))
            self._plan_attempt = attempt
        return self.plans[attempt]

    def _declare_subtasks(self, rows: Any) -> None:
        for row in _rows(rows):
            name = row.get("name")
            if not isinstance(name, str):
                continue
            lane = self._subtask(name)
            lane.node.meta.update(_fields(row, "depends_on", "paths"))
            self._declare_attempts(lane, row.get("best_of"))

    def _declare_attempts(self, lane: _Lane, best_of: Any) -> None:
        if isinstance(best_of, dict) and type(best_of.get("n")) is int:
            for number in range(1, min(5, best_of["n"]) + 1):
                self._attempt(lane, number)

    def _attempt(self, parent: _Lane, number: int) -> _Lane:
        if number not in parent.attempts:
            node = self._node(parent, "attempt", f"候选 #{number}", number, attempt=number)
            parent.attempts[number] = _Lane(node)
            self.graph.link(parent.node, node, "spawn")
        return parent.attempts[number]

    def _lane(self, event: Event) -> _Lane:
        payload = event.payload
        lane = self.root
        name = payload.get("subtask")
        if isinstance(name, str):
            if self._plan_attempt is not None and name == f"plan-{self._plan_attempt}":
                plan = self.plans[self._plan_attempt]
                if plan.node.finished_at is None or payload.get("turn") == 0:
                    return plan
            lane = self._subtask(name)
            if event.type != "subtasks.merged":
                lane.node.start(event.created_at)
        number = payload.get("best_of_attempt", payload.get("best_of_candidate"))
        if type(number) is int:
            lane = self._attempt(lane, number)
            lane.node.start(event.created_at)
        return lane

    def _plan_event(self, event: Event) -> None:
        p, at = event.payload, event.created_at
        lane = self._plan(p.get("attempt") or 1, at)
        if event.type == "plan.started":
            lane.node.start(at)
        else:
            lane.node.meta.update(_fields(p, "reason", "split", "parallelism", "max_parallel_agents"))
            lane.node.close("succeeded" if event.type == "plan.completed" else "failed", at)
            self.root.tail = lane.tail
            self.plan_gate = lane.tail

    def _turn_event(self, lane: _Lane, event: Event) -> DAGNode:
        p, at, type_ = event.payload, event.created_at, event.type
        number = p.get("turn")
        if type(number) is not int:
            number = max(lane.turns, default=1)
        turn = lane.turns.get(number)
        if type_ == "agent.turn_started" or turn is None:
            retry = turn is not None and type_ == "agent.turn_started"
            if retry and turn.status == "running":
                self._close_turn(lane, turn, "failed", at)
            fix = lane.needs_fix or (retry and turn.kind == "fix")
            kind: NodeKind = "fix" if fix else "turn"
            label = "修复" if fix else "执行"
            turn = self._node(lane, kind, f"{label} · 第 {number} 轮", event.seq, turn=number,
                              **_fields(p, "backend", "attempt", "model", "worker"))
            lane.turns[number] = self.graph.advance(lane, turn)
            lane.needs_fix = False
            if type_ != "agent.turn_completed":
                turn.start(at)
        if type_ in TOOL_EVENTS:
            self._tool(lane, turn, event)
        elif type_ == "agent.usage":
            tokens = _tokens(p)
            cost = _number(p.get("cost_usd", p.get("cost")))
            if tokens is not None:
                turn.tokens = (turn.tokens or 0) + tokens
            if cost is not None:
                turn.cost_usd = cost if p.get("source") == "model_gateway" else (turn.cost_usd or 0) + cost
        elif type_ == "agent.turn_completed":
            cost = _number(p.get("cost_usd"))
            if cost is not None:
                turn.cost_usd = cost
            self._close_turn(lane, turn, "succeeded", at)
        elif type_ == "agent.turn_interrupted":
            self._close_turn(lane, turn, "cancelled", at)
        elif type_ in {"agent.error", "model.provider_error"}:
            turn.meta.update(_fields(p, "message", "kind", "http_status"))
            self._close_turn(lane, turn, "failed", at)
        return turn

    def _tool(self, lane: _Lane, turn: DAGNode, event: Event) -> None:
        group = lane.tools.get(turn.id)
        if group is None:
            group = self.graph.add(
                self.task, f"{turn.id}/tool_group", "tool_group", "工具调用",
                tools=[], counts={}, **_fields(turn.meta, "parent", "turn", "subtask"),
            )
            lane.tools[turn.id] = self.graph.advance(lane, group)
            group.start(event.created_at)
        p = event.payload
        name = p.get("name")
        if not isinstance(name, str) or not name:
            name = event.type.removeprefix("agent.")
        tool = {"name": name, "type": event.type, "seq": event.seq, "failed": (
            p.get("is_error") is True or p.get("status") in ("error", "failed")
            or p.get("exit_code") not in (None, 0)
        ), **_fields(p, "command", "exit_code", "duration_ms")}
        group.meta["tools"].append(tool)
        counts = group.meta["counts"]
        counts[name] = counts.get(name, 0) + 1
        group.label = f"工具调用 · {len(group.meta['tools'])}"

    @staticmethod
    def _close_turn(lane: _Lane, turn: DAGNode, status: Status, at: datetime | None) -> None:
        turn.close(status, at)
        if group := lane.tools.get(turn.id):
            failed = sum(tool["failed"] for tool in group.meta["tools"])
            group.meta["failed_count"] = failed
            group.close("failed" if failed else status, at)

    def _check(self, lane: _Lane, event: Event) -> DAGNode:
        p, at, type_ = event.payload, event.created_at, event.type
        number = p.get("turn")
        if type(number) is not int:
            number = max(lane.turns, default=0)
        stage, label, kind = {
            "verification": ("commands", "验证", "verification"),
            "intents": ("intents", "验证", "verification"),
            "acceptance": ("acceptance", "隐藏验收", "verification"),
            "scope": ("scope", "路径范围检查", "verification"),
            "ci": ("ci", "持续集成", "ci"),
        }[type_.split(".", 1)[0]]
        grouping = "verify" if stage in ("commands", "intents") else stage
        key = (grouping, number)
        node = lane.checks.get(key)
        if node is None:
            node = self._node(lane, kind, label, event.seq, turn=number, stages={})
            lane.checks[key] = self.graph.advance(lane, node)
        info = _fields(p, "status", "passed", "skipped", "checks", "exit_code", "log_artifact", "reason",
                       "commit", "summary", "timed_out", "url", "paths", "command")
        if isinstance(info.get("checks"), list):
            info["checks"] = [_fields(row, "name", "command", "passed", "exit_code", "duration_s")
                              for row in _rows(info["checks"])]
        if type_ in {"verification.started", "ci.requested"}:
            node.started_at = node.started_at or at
            node.meta["stages"][stage] = {**info, "status": "running"}
            node.close("running", None)
            return node
        if stage == "intents" and "passed" not in info:
            checks = _rows(info.get("checks"))
            info["passed"] = all(check.get("passed") is True for check in checks)
            info["skipped"] = not checks
        node.meta["stages"][stage] = {**info, "status": _status(info)}
        statuses = {entry["status"] for entry in node.meta["stages"].values()}
        lane.needs_fix = "failed" in statuses
        if "running" not in statuses:
            status = "failed" if lane.needs_fix else ("succeeded" if "succeeded" in statuses else "skipped")
            node.close(status, at)
        return node

    def _best_of(self, lane: _Lane, type_: str, p: dict, at: datetime | None) -> None:
        if type_ == "best_of.started":
            for row in _rows(p.get("attempts")):
                number = row.get("attempt")
                if type(number) is int:
                    self._attempt(lane, number).node.meta.update(_fields(row, "model", "agent_version"))
            return
        number = p.get("attempt")
        if type(number) is not int:
            return
        attempt = self._attempt(lane, number).node
        if type_ == "best_of.attempt_completed":
            attempt.meta.update(_fields(p, "reason", "model", "agent_version", "patch_artifact", "turns"))
            attempt.cost_usd = _number(p.get("cost_usd"))
            verification = p.get("verification")
            passed = isinstance(verification, dict) and verification.get("passed") is True
            attempt.close("succeeded" if passed else "failed", at)
        else:
            select = self._selection(lane)
            if lane.tail is not lane.node:
                self.graph.advance(lane, select)
            else:
                lane.tail = select
            if at is not None:
                select.start(at)
            select.meta.update(_fields(p, "attempt", "actor", "commit"))
            select.label = f"选择候选 #{number}"
            select.close("succeeded", at)
            attempt.meta["selected"] = True
            if lane is not self.root:
                lane.node.close(attempt.status if attempt.status != "pending" else "succeeded", at)

    def _selection(self, lane: _Lane) -> DAGNode:
        if lane.select is None:
            lane.select = self._node(lane, "merge", "选择候选", "select", action="select")
        return lane.select

    def _merge(self, rows: Any, at: datetime | None) -> None:
        rows = _rows(rows)
        if not rows:
            return
        self._declare_subtasks(rows)
        self.merge = self._node(self.root, "merge", "合并子任务", "subtasks", action="merge")
        self.graph.advance(self.root, self.merge)
        if at is not None:
            self.merge.start(at)
        counts = Counter()
        for row in rows:
            lane = self.subtasks.get(row.get("name"))
            if lane is None:
                continue
            state = row.get("status")
            counts[state or "failed"] += 1
            lane.node.meta.update(merge_status=state, **_fields(row, "reason", "conflicts", "commit"))
            status = "succeeded" if state == "conflict" and row.get("passed") else _status(row)
            lane.node.close(status, lane.node.finished_at or at)
        self.merge.meta["counts"] = dict(counts)
        self.merge.close("failed" if counts.keys() - {"merged", "unchanged"} else "succeeded", at)

    def _message(self, event: Event) -> DAGNode:
        p, at = event.payload, event.created_at
        name = p.get("message_id") or str(event.seq)
        node = self.messages.get(name)
        if node is None:
            node = self.messages[name] = self._node(
                self.root, "interjection", "用户插话", event.seq, message_id=name,
            )
            self.graph.link(self.root.tail, node)
        node.meta.update(_fields(p, "mode", "actor", "text", "turn", "plan_attempt"))
        if event.type == "user.message":
            node.meta["queued_at"] = at.isoformat()
        elif event.type == "user.message.delivered":
            node.start(at)
        elif event.type == "user.message.applied":
            node.close("succeeded", at)
        return node

    def _request_review(self, at: datetime | None, p: dict) -> None:
        is_plan = "plan" in p
        if self.review and self.review.status == "running":
            self.review.close("skipped", at)
        suffix = self._review_count
        self._review_count += 1
        self.review = self.graph.advance(self.root, self._node(
            self.root, "review", "审核规划" if is_plan else "审核结果", suffix,
            stage="plan" if is_plan else "result", **_fields(p, "turn", "attempt", "verification_passed"),
        ))
        self.review.status, self.review.started_at = "running", at
        self.root.needs_fix = False
        if is_plan:
            self.plan_gate = self.review

    def _state_event(self, event: Event) -> None:
        p, at = event.payload, event.created_at
        state = p.get("to")
        if state in TERMINAL_STATES:
            self.finished_at = at
            if at >= self.task.updated_at:
                self.state = TaskState(state)
        elif state in (TaskState.PROVISIONING, TaskState.RUNNING, TaskState.VERIFYING):
            self.root.node.start(at)
            if p.get("from") == TaskState.AWAITING_REVIEW and self.review and self.review.status == "running":
                self.review.close("skipped", at)
        elif state == TaskState.AWAITING_REVIEW:
            best_of = self.task.spec.get("best_of") or {}
            if best_of.get("select") == "human" and (
                self.root.select is None or self.root.select.status != "succeeded"
            ):
                self._selection(self.root).start(at)
            elif self.review is None or self.review.status != "running":
                self._request_review(at, {})

    def _pr(self, p: dict, at: datetime | None, status: Status = "succeeded") -> None:
        if self.pr is None:
            self.pr = self.graph.advance(self.root, self._node(self.root, "pr", "拉取请求", "pr"))
            if at is not None:
                self.pr.start(at)
        if at is not None or self.pr.status == "pending":
            self.pr.close(status, at or self.pr.finished_at)
        self.pr.meta.update(_fields(p, "number", "state", "url", "head", "base", "message"))
        if p.get("number"):
            self.pr.label = f"拉取请求 #{p['number']}"

    def _read_event(self, event: Event) -> DAGNode | None:
        p, at, type_ = event.payload, event.created_at, event.type
        if type_ in {"plan.started", "plan.completed", "plan.rejected"}:
            self._plan_event(event)
            return self.plans[p.get("attempt") or 1].node
        elif type_ == "task.state_changed":
            self._state_event(event)
            return self.finish if p.get("to") in TERMINAL_STATES else self.root.node
        elif type_ == "review.requested":
            self._request_review(at, p)
            return self.review
        elif type_ == "review.submitted":
            if self.review is None or self.review.status != "running":
                self._request_review(None, p)
            self.review.meta.update(_fields(p, "decision", "actor", "feedback"))
            self.review.close("failed" if p.get("decision") == "reject" else "succeeded", at)
            return self.review
        elif type_ == "subtasks.merged":
            if self.merge is None:
                self._merge(p.get("subtasks"), at)
        elif type_ == "subtasks.scheduled":
            self.root.node.meta.update(_fields(p, "max_parallel_agents"))
        elif type_ in {"subtask.queued", "subtask.started", "subtask.completed"} and isinstance(
            name := p.get("subtask"), str,
        ):
            node = self._subtask(name).node
            node.meta.update(_fields(p, "backend", "model"))
            if type_ == "subtask.queued":
                node.meta["waiting_for_slot"] = True
            else:
                node.meta.pop("waiting_for_slot", None)
                if type_ == "subtask.started":
                    node.start(at)
                else:
                    node.close(_status(p), at)
            return node
        elif type_.startswith("user.message"):
            return self._message(event)
        elif type_ == "pr.opened":
            self._pr(p, at)
        elif type_ == "scm.failed" and self.task.spec.get("pull_request") and p.get("phase") != "terminal":
            self._pr(p, at, "failed")
        else:
            lane = self._lane(event)
            if type_ in TURN_EVENTS:
                return self._turn_event(lane, event)
            elif type_ in {"verification.started", "verification.completed", "intents.checked",
                           "acceptance.completed", "scope.violation", "ci.requested", "ci.completed"}:
                return self._check(lane, event)
            elif type_ in {"best_of.started", "best_of.attempt_completed", "best_of.selected"}:
                self._best_of(lane, type_, p, at)
                if type_ == "best_of.selected":
                    return lane.select
                attempt = lane.attempts.get(p.get("attempt"))
                return attempt.node if attempt else lane.node
            elif type_ == "sandbox.destroyed" and lane.node.kind == "subtask" and not lane.attempts:
                if lane.tail.kind == "verification":
                    status = "succeeded" if lane.tail.status == "skipped" else lane.tail.status
                    lane.node.close(status, at)
                elif lane.tail.status == "failed":
                    lane.node.close("failed", at)
        return None

    def read(self, focus: dict[int, DAGNode] | None = None) -> None:
        self._declare_subtasks(self.task.spec.get("subtasks"))
        self._declare_attempts(self.root, self.task.spec.get("best_of"))
        for row in _rows(self.task.result.get("best_of_plan")):
            if type(number := row.get("attempt")) is int:
                self._attempt(self.root, number).node.meta.update(_fields(row, "model", "agent_version"))

        latest_plan = next((e for e in reversed(self.events)
                            if e.type in {"plan.started", "plan.completed", "plan.rejected"}), None)
        if latest_plan and latest_plan.type == "plan.completed" and latest_plan.payload.get("split"):
            self._declare_subtasks(latest_plan.payload.get("subtasks"))
            self.root.node.meta.update(_fields(latest_plan.payload, "parallelism", "max_parallel_agents"))
        if latest_plan is None and self.task.spec.get("plan") == "auto" and self.state in (
            TaskState.QUEUED, TaskState.PROVISIONING,
        ):
            self._plan(1)
        elif self.subtasks and self.task.spec.get("plan") != "auto":
            plan = self._node(self.root, "plan", "执行计划", "manual", split=True)
            plan.close("succeeded", self.task.created_at)
            self.plan_gate = self.graph.advance(self.root, plan)
        for event in self.events:
            node = self._read_event(event)
            if focus is not None and node is not None:
                focus[event.seq] = node
        if self._plan_attempt is not None:
            lane = self.plans[self._plan_attempt]
            if self.root.tail is lane.node:
                self.root.tail = lane.tail

        if self.merge is None:
            self._merge(self.task.result.get("subtasks"), None)
        if pr := self.task.result.get("pr"):
            self._pr(pr, None)
        for number, lane in self.root.attempts.items():
            candidate = self.task.result.get(f"best_of_candidate_{number}")
            if candidate and lane.node.finished_at is None:
                self._best_of(self.root, "best_of.attempt_completed", {"attempt": number, **candidate}, None)
        if (self.root.select is None or self.root.select.status != "succeeded") and type(
            self.task.result.get("best_of_chosen")
        ) is int:
            self._best_of(self.root, "best_of.selected", {"attempt": self.task.result["best_of_chosen"]}, None)
        if self.state == TaskState.AWAITING_REVIEW and not self.review:
            if (self.task.spec.get("best_of") or {}).get("select") == "human" and (
                self.root.select is None or self.root.select.status != "succeeded"
            ):
                self._selection(self.root).status = "running"
            else:
                self._request_review(None, {})

    def _wire_lane(self, lane: _Lane, join: DAGNode, edge: EdgeKind = "sequence") -> None:
        if lane.attempts:
            select = self._selection(lane)
            for attempt in lane.attempts.values():
                self.graph.link(attempt.tail, select, "select")
                turns = [n for n in self.graph.nodes.values() if n.id.startswith(attempt.node.id + "/")
                         and n.kind in ("turn", "fix")]
                attempt.node.tokens = self._total(turns, "tokens")
                if attempt.node.cost_usd is None:
                    attempt.node.cost_usd = self._total(turns, "cost_usd")
        self.graph.link(lane.output, join, edge)

    @staticmethod
    def _total(nodes: Iterable[DAGNode], attr: str) -> Any:
        values = [value for node in nodes if (value := getattr(node, attr)) is not None]
        return sum(values) if values else None

    def wire(self) -> None:
        after_subtasks = self.merge or (
            self.root.tail if self.root.tail.id != getattr(self.plan_gate, "id", None) else self.finish
        )
        if after_subtasks is self.root.node:
            after_subtasks = self.finish
        # Allocate pending selection nodes before wiring dependencies to each branch's output.
        for lane in [self.root, *self.subtasks.values()]:
            if lane.attempts:
                self._selection(lane)
        for name, lane in self.subtasks.items():
            self.graph.link(self.plan_gate or self.root.node, lane.node, "spawn")
            needs = lane.node.meta.get("depends_on")
            for need in needs if isinstance(needs, list) else []:
                if not isinstance(need, str):
                    continue
                dependency = self.subtasks.get(need)
                if dependency is not None:
                    self.graph.link(dependency.node, lane.node, "dependency")
                    child = self.subtask_links.get(need)
                    source = child.finish if child else dependency.output
                    if source is not dependency.node:
                        self.graph.link(source, lane.node)
            child = self.subtask_links.get(name)
            edge: EdgeKind = "merge" if self.merge else "sequence"
            if child:
                self._wire_lane(lane, child.root.node, "spawn")
                self.graph.link(child.finish, after_subtasks, edge)
            else:
                self._wire_lane(lane, after_subtasks, edge)
        self._wire_lane(self.root, self.finish)
        for node in self.messages.values():
            plan = self.plans.get(node.meta.get("plan_attempt"))
            target = plan.node if plan else self.root.turns.get(node.meta.get("turn"))
            if target:
                self.graph.link(node, target)
            elif self.state in TERMINAL_STATES:
                self.graph.link(node, self.finish)
        self._subtask_lanes()

    def _subtask_lanes(self) -> None:
        nodes = {lane.node.id: lane.node for lane in self.subtasks.values()}
        if not nodes:
            return
        self.root.node.meta["subtask_count"] = len(nodes)
        dependencies: dict[str, list[str]] = {id_: [] for id_ in nodes}
        for edge in self.graph.edges.values():
            if edge.kind == "dependency" and edge.source in nodes and edge.target in nodes:
                dependencies[edge.target].append(edge.source)
        levels: dict[str, int] = {}
        lanes: Counter[int] = Counter()
        for id_ in TopologicalSorter(dependencies).static_order():
            level = 1 + max((levels[need] for need in dependencies[id_]), default=-1)
            levels[id_] = level
            nodes[id_].meta.update(level=level, lane=lanes[level])
            lanes[level] += 1

    def finalize(self, owned: list[DAGNode]) -> None:
        state = self.state
        ending = self.finished_at or self.task.updated_at
        node = self.root.node
        node.meta.update(state=state, **_fields({"reason": self.task.state_reason}, "reason"))
        status = TASK_STATUS.get(state, "running")
        node.status = status
        if state in TERMINAL_STATES:
            node.close(status, ending)
            self.finish.start(ending)
            self.finish.close(status, ending)
            self.finish.meta.update(_fields(node.meta, "reason"))
            for step in owned:
                if step.status not in {"pending", "running"}:
                    continue
                if step.kind == "review" and self.task.spec.get("auto_finish"):
                    step.close("skipped", ending)
                elif step.status == "pending":
                    step.close("cancelled" if status == "cancelled" else "skipped", None)
                else:
                    step.close("cancelled" if status == "cancelled" else (
                        "skipped" if status == "succeeded" else "failed"
                    ), ending)

        turns = [n for n in owned if n.kind in ("turn", "fix")]
        node.tokens = _tokens(self.task.usage)
        if node.tokens is None:
            node.tokens = self._total(turns, "tokens")
        micro = _number(self.task.usage.get("cost_micro_usd"))
        node.cost_usd = micro / 1_000_000 if micro is not None else self._total(turns, "cost_usd")
        for plan in self.plans.values():
            plan_turns = [turn for turn in turns if turn.id.startswith(plan.node.id + "/")]
            plan.node.tokens = self._total(plan_turns, "tokens")
            plan.node.cost_usd = self._total(plan_turns, "cost_usd")
        for lane in self.subtasks.values():
            branch = lane.node
            branch_turns = [turn for turn in turns if turn.id.startswith(branch.id + "/")]
            branch.tokens = self._total(branch_turns, "tokens")
            branch.cost_usd = self._total(branch_turns, "cost_usd")


def build_task_dag(task: Task, events: Iterable[Event], children: Iterable[Task]) -> TaskDAG:
    """Build a snapshot from the root, the available descendant rows and their events."""
    tasks = {task.id: task}
    tasks.update({child.id: child for child in children
                  if child.tenant_id == task.tenant_id and child.id != task.id})
    grouped = defaultdict(list)
    for event in events:
        if event.task_id in tasks:
            grouped[event.task_id].append(event)

    graph = _Graph()
    traces = {
        row.id: _TaskTrace(graph, row, sorted(grouped[row.id], key=lambda e: e.seq))
        for row in tasks.values()
    }
    for trace in traces.values():
        trace.read()

    links = {}
    for parent in traces.values():
        for child_id, name in linked_tasks(parent.task, parent.events).items():
            links.setdefault(child_id, (parent.task.id, name))
    root = traces[task.id]
    for child_id, child in traces.items():
        if child is root:
            continue
        persisted = _uuid(child.task.result.get("parent_task_id"))
        parent_id, name = links.get(child_id, (persisted, child.task.result.get("subtask")))
        parent = traces.get(parent_id, root)
        if parent is child:
            parent = root
        if isinstance(name, str) and name not in parent.subtask_links:
            parent.subtask_links[name] = child
            parent._subtask(name).node.meta["child_task_id"] = str(child_id)
        else:
            graph.link(parent.root.node, child.root.node, "spawn")
            graph.link(child.finish, parent.finish)
        child.root.node.meta["parent_task_id"] = str(parent.task.id)
    for trace in traces.values():
        trace.wire()

    owned = defaultdict(list)
    for node in graph.nodes.values():
        owned[node.task_id].append(node)
    for trace in traces.values():
        trace.finalize(owned[trace.task.id])
    for trace in traces.values():
        for name, child in trace.subtask_links.items():
            branch, actual = trace.subtasks[name].node, child.root.node
            branch.tokens, branch.cost_usd = actual.tokens, actual.cost_usd
            branch.started_at = actual.started_at or branch.started_at
            branch.close(actual.status, actual.finished_at)
            # Include a persisted child task's execution in the logical subtask's summary.
            for step in owned[child.task.id]:
                if step is actual or step is child.finish or step.meta.get("parent") == actual.id:
                    step.meta["parent"] = branch.id
    return TaskDAG(nodes=list(graph.nodes.values()), edges=list(graph.edges.values()))


# Task conversation
TASK_MILESTONES = frozenset({
    "task.created", "task.state_changed", "plan.started", "plan.completed", "plan.rejected",
    "agent.turn_started", "agent.turn_completed", "agent.turn_interrupted",
    "verification.completed", "acceptance.completed", "ci.completed", "review.requested",
    "best_of.started", "best_of.attempt_completed", "best_of.selected",
    "subtask.started", "subtask.completed",
    "user.message", "user.message.delivered", "user.message.applied",
})
TASK_MILESTONE_CONTEXT = TASK_MILESTONES | TURN_EVENTS | {
    "subtask.queued", "subtasks.merged", "subtasks.scheduled", "review.submitted", "verification.started",
    "intents.checked", "scope.violation", "ci.requested", "sandbox.destroyed", "pr.opened", "scm.failed",
}


def task_conversation_nodes(task: Task, events: list[Event]) -> tuple[dict[int, DAGNode], dict[str, DAGNode]]:
    """Replay the graph's node IDs and tool summaries without building child task graphs."""
    trace = _TaskTrace(_Graph(), task, events)
    focus: dict[int, DAGNode] = {}
    trace.read(focus)
    return focus, trace.graph.nodes
