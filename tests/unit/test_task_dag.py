from datetime import UTC, datetime, timedelta
from graphlib import TopologicalSorter
from uuid import uuid4

from sca.core.models import Event, Task, TaskState
from sca.core.task_dag import build_task_dag

NOW = datetime(2026, 11, 21, tzinfo=UTC)


def task(state=TaskState.RUNNING, **fields):
    return Task(
        id=uuid4(), tenant_id=fields.pop("tenant_id", "demo"), kind="coding",
        spec=fields.pop("spec", {}), state=state, created_at=NOW,
        updated_at=NOW + timedelta(seconds=80), **fields,
    )


def history(owner, *rows):
    return [
        Event(task_id=owner.id, seq=i, type=type_, payload=payload,
              created_at=NOW + timedelta(seconds=i))
        for i, (type_, payload) in enumerate(rows, 1)
    ]


def nodes(graph, kind):
    return [node for node in graph.nodes if node.kind == kind]


def linked(graph, source, target, kind="sequence"):
    return any(edge.source == source.id and edge.target == target.id and edge.kind == kind
               for edge in graph.edges)


def assert_dag(graph):
    sorter = TopologicalSorter()
    ids = {node.id for node in graph.nodes}
    assert len(ids) == len(graph.nodes)
    for node in ids:
        sorter.add(node)
    for edge in graph.edges:
        assert edge.source in ids and edge.target in ids
        sorter.add(edge.target, edge.source)
    assert len(list(sorter.static_order())) == len(ids)


def test_dependency_diamond_has_stable_pending_nodes():
    owner = task(spec={"plan": "auto"})
    plan = [
        {"name": "schema", "paths": ["schema/**"]},
        {"name": "fx", "depends_on": ["schema"], "best_of": {"n": 3}},
        {"name": "tax", "depends_on": ["schema"]},
        {"name": "pricing", "depends_on": ["fx", "tax"]},
    ]
    events = history(
        owner, ("plan.started", {"attempt": 1}),
        ("agent.turn_started", {"subtask": "plan-1", "turn": 0}),
        ("agent.turn_completed", {"subtask": "plan-1", "turn": 0}),
        ("plan.completed", {"attempt": 1, "split": True, "subtasks": plan}),
        ("subtask.started", {"subtask": "schema"}),
        ("agent.turn_started", {"subtask": "schema", "turn": 1}),
        ("agent.turn_completed", {"subtask": "schema", "turn": 1}),
        ("verification.completed", {"subtask": "schema", "turn": 1, "passed": True}),
        ("sandbox.destroyed", {"subtask": "schema"}),
        ("subtask.started", {"subtask": "fx"}),
        ("best_of.started", {"subtask": "fx", "attempts": [{"attempt": i} for i in range(1, 4)]}),
        ("sandbox.destroyed", {"subtask": "fx"}),
        ("workspace.forked", {"subtask": "fx", "best_of_attempt": 2}),
    )
    before = build_task_dag(owner, events[:4], [])
    graph = build_task_dag(owner, reversed(events), [])
    assert_dag(graph)
    branches = {node.label: node for node in nodes(graph, "subtask")}
    assert [node.id for node in nodes(before, "subtask")] == [node.id for node in branches.values()]
    assert {label: node.status for label, node in branches.items()} == {
        "schema": "succeeded", "fx": "running", "tax": "pending", "pricing": "pending",
    }
    assert {i.meta["attempt"]: i.status for i in nodes(graph, "attempt")} == {
        1: "pending", 2: "running", 3: "pending",
    }
    for name in ("fx", "tax"):
        assert linked(graph, branches["schema"], branches[name], "dependency")
        assert linked(graph, branches[name], branches["pricing"], "dependency")
    assert len(nodes(graph, "plan")) == 1
    assert all(node.status == "pending" for node in nodes(graph, "finish"))


def test_attempt_scopes_allocate_pick_and_count_only_their_turns():
    owner = task(spec={"best_of": {"n": 3, "select": "human"}})
    rows = history(
        owner,
        ("best_of.started", {"attempts": [{"attempt": i, "model": "fake"} for i in range(1, 4)]}),
        ("agent.turn_started", {"best_of_attempt": 1, "attempt": 1, "turn": 1}),
        ("agent.turn_started", {"best_of_attempt": 2, "attempt": 1, "turn": 1}),
        ("agent.usage", {"best_of_attempt": 2, "turn": 1, "input_tokens": 10, "output_tokens": 5}),
        ("agent.usage", {"best_of_attempt": 1, "turn": 1, "input_tokens": 2, "output_tokens": 2}),
        ("agent.turn_completed", {"best_of_attempt": 1, "turn": 1, "cost_usd": .12, "cost_final": True}),
        ("best_of.attempt_completed", {"attempt": 1, "verification": {"passed": False}, "cost_usd": .12}),
        ("agent.turn_completed", {"best_of_attempt": 2, "turn": 1}),
        ("best_of.attempt_completed", {"attempt": 2, "verification": {"passed": True, "skipped": True}}),
        ("best_of.attempt_completed", {"attempt": 3, "reason": "budget exhausted"}),
        ("task.state_changed", {"to": "AWAITING_REVIEW"}),
        ("best_of.selected", {"attempt": 2, "actor": "reviewer"}),
        ("review.requested", {"turn": 1}),
    )
    waiting = build_task_dag(owner, rows[:-2], [])
    assert nodes(waiting, "merge")[0].status == "running"
    assert not nodes(waiting, "review")
    graph = build_task_dag(owner, rows, [])
    assert_dag(graph)
    attempts = nodes(graph, "attempt")
    assert [n.status for n in attempts] == ["failed", "succeeded", "failed"]
    assert [n.tokens for n in attempts] == [4, 15, None]
    assert [n.meta.get("selected") for n in attempts] == [None, True, None]
    assert attempts[0].cost_usd == .12
    select, = nodes(graph, "merge")
    assert select.meta == {"action": "select", "attempt": 2, "actor": "reviewer"}
    assert sum(edge.kind == "select" and edge.target == select.id for edge in graph.edges) == 3
    assert linked(graph, select, nodes(graph, "review")[0])


def test_fix_tools_cost_and_interjection_are_linked_to_the_correct_turn():
    owner = task(TaskState.SUCCEEDED, usage={"input_tokens": 11, "output_tokens": 4, "cost_micro_usd": 2000})
    rows = history(
        owner,
        ("task.state_changed", {"to": "RUNNING"}),
        ("agent.turn_started", {"turn": 1}),
        ("agent.tool", {"turn": 1, "name": "read", "duration_ms": 10}),
        ("agent.tool", {"turn": 1, "name": "read"}),
        ("agent.command", {"turn": 1, "command": "check", "exit_code": 1}),
        ("agent.usage", {"turn": 1, "input_tokens": 7, "output_tokens": 2, "cost": .0011}),
        ("agent.turn_completed", {"turn": 1, "cost_usd": .001, "cost_final": True}),
        ("verification.started", {"turn": 1, "checks": 1}),
        ("verification.completed", {"turn": 1, "passed": False, "checks": [
            {"command": "check", "passed": False, "output_tail": "very long raw output"},
        ]}),
        ("user.message", {"message_id": "m1", "text": "Check rounding", "mode": "queue"}),
        ("user.message.delivered", {"message_id": "m1", "turn": 2}),
        ("agent.turn_started", {"turn": 2}),
        ("agent.usage", {"turn": 2, "input_tokens": 4, "output_tokens": 2}),
        ("agent.turn_completed", {"turn": 2, "cost_usd": .001}),
        ("user.message.applied", {"message_id": "m1", "turn": 2}),
        ("verification.completed", {"turn": 2, "passed": True}),
        ("ci.requested", {"turn": 2, "commit": "abc"}),
        ("ci.completed", {"turn": 2, "passed": True, "commit": "abc"}),
        ("review.requested", {"turn": 2, "verification_passed": True}),
        ("pr.opened", {"number": 7, "state": "open"}),
        ("review.submitted", {"decision": "approve"}),
        ("task.state_changed", {"to": "SUCCEEDED"}),
    )
    graph = build_task_dag(owner, rows, [])
    assert_dag(graph)
    turn, = nodes(graph, "turn")
    tools, = nodes(graph, "tool_group")
    first, second = nodes(graph, "verification")
    fix, = nodes(graph, "fix")
    message, = nodes(graph, "interjection")
    assert turn.tokens == 9 and turn.cost_usd == .001 and turn.duration_s == 5
    assert tools.meta["counts"] == {"read": 2, "command": 1}
    assert tools.meta["failed_count"] == 1
    assert first.status == "failed" and first.meta["stages"]["commands"]["checks"] == [
        {"command": "check", "passed": False},
    ]
    assert fix.status == message.status == second.status == "succeeded"
    for source, target in ((turn, tools), (tools, first), (first, fix), (message, fix), (fix, second)):
        assert linked(graph, source, target)
    assert len(nodes(graph, "ci")) == len(nodes(graph, "pr")) == len(nodes(graph, "review")) == 1
    assert nodes(graph, "finish")[0].finished_at == rows[-1].created_at
    assert nodes(graph, "task")[0].tokens == 15


def test_old_task_without_started_events_does_not_invent_timings():
    owner = task(TaskState.SUCCEEDED, result={"pr": {"number": 8, "state": "merged"}})
    graph = build_task_dag(owner, history(
        owner, ("agent.turn_completed", {"turn": 1, "cost_final": True}),
        ("verification.completed", {"passed": True, "skipped": True}),
        ("review.submitted", {"decision": "approve"}),
    ), [])
    assert_dag(graph)
    turn, = nodes(graph, "turn")
    assert turn.status == "succeeded" and turn.started_at is None and turn.duration_s is None
    assert turn.cost_usd is None
    assert nodes(graph, "verification")[0].status == "skipped"
    assert nodes(graph, "pr")[0].meta["state"] == "merged"
    assert nodes(graph, "finish")[0].status == "succeeded"


def test_cancelled_task_closes_open_turns_and_queued_messages():
    owner = task(TaskState.CANCELLED, spec={"subtasks": [{"name": "first"}, {"name": "next"}]})
    rows = history(
        owner, ("subtask.started", {"subtask": "first"}),
        ("agent.turn_started", {"subtask": "first", "turn": 1}),
        ("agent.command", {"subtask": "first", "turn": 1, "command": "check"}),
        ("user.message", {"message_id": "m1"}),
        ("task.state_changed", {"to": "CANCELLED"}),
    )
    graph = build_task_dag(owner, rows, [])
    assert_dag(graph)
    for kind in ("turn", "tool_group", "interjection", "finish", "subtask"):
        assert {step.status for step in nodes(graph, kind)} == {"cancelled"}


def test_real_descendants_link_recursively_and_ignore_another_tenant():
    owner = task(spec={"subtasks": [{"name": "report"}]})
    child = task(TaskState.SUCCEEDED, result={"parent_task_id": str(owner.id), "subtask": "report"})
    grandchild = task(TaskState.FAILED, result={"parent_task_id": str(child.id), "subtask": "render"})
    other = task(tenant_id="other")
    graph = build_task_dag(owner, [
        *history(owner, ("task.child_started", {"task_id": str(other.id)})),
        *history(grandchild, ("agent.turn_started", {"turn": 3})),
    ], [grandchild, child, other])
    assert_dag(graph)
    assert {node.task_id for node in graph.nodes} == {owner.id, child.id, grandchild.id}
    branches = {node.label: node for node in nodes(graph, "subtask")}
    branch, render = branches["report"], branches["render"]
    actual = {node.task_id: node for node in nodes(graph, "task")}
    assert branch.meta["child_task_id"] == str(child.id)
    assert branch.status == "succeeded"
    assert linked(graph, branch, actual[child.id], "spawn")
    assert render.status == "failed"
    assert linked(graph, actual[child.id], render, "spawn")
    assert linked(graph, render, actual[grandchild.id], "spawn")


def test_legacy_cycle_does_not_break_topological_layout():
    owner = task(spec={"subtasks": [
        {"name": "a", "depends_on": ["b", "missing"]}, {"name": "b", "depends_on": ["a"]},
    ]})
    graph = build_task_dag(owner, [], [])
    assert_dag(graph)
    assert sum(edge.kind == "dependency" for edge in graph.edges) == 1


def test_parallel_intent_checks_and_command_verification_share_a_node():
    owner = task()
    rows = history(
        owner, ("agent.turn_completed", {"turn": 1}),
        ("intents.checked", {"turn": 1, "checks": [{"name": "contract", "passed": True}]}),
        ("verification.started", {"turn": 1, "checks": 1}),
        ("verification.completed", {"turn": 1, "passed": True}),
        ("ci.requested", {"turn": 1, "commit": "abc"}),
    )
    staged, = nodes(build_task_dag(owner, rows[:3], []), "verification")
    assert staged.status == "running" and staged.finished_at is None
    verify, = nodes(build_task_dag(owner, rows, []), "verification")
    assert verify.status == "succeeded" and verify.duration_s == 1
    assert {name: stage["status"] for name, stage in verify.meta["stages"].items()} == {
        "intents": "succeeded", "commands": "succeeded",
    }
