from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert, Update

from sca.evolve.dag import build_evolution_dag
from scripts.evo_dag_demo import seed_demo

NOW = datetime(2026, 11, 22, tzinfo=UTC)


def run(offset, split="dev", overlay=None, status="completed"):
    return {"id": uuid4(), "variant": {"split": split, "variant": {"agent_overlay": overlay or {}}},
            "status": status, "created_at": NOW + timedelta(seconds=offset), "finished_at": None}


def candidate(offset, generation, *, run_id=None, decision="pending"):
    return {"id": uuid4(), "generation": generation, "created_at": NOW + timedelta(seconds=offset),
            "hypothesis": "Check boundaries once before editing", "overlay": {"tool_guidance": str(offset)},
            "dev_run_id": run_id, "decision": decision}


def task(run_row, *, state="SUCCEEDED", name="fixture"):
    return {"id": uuid4(), "state": state, "created_at": run_row["created_at"], "updated_at": NOW,
            "eval_run_id": str(run_row["id"]), "idempotency_key": f"eval:{run_row['id']}:{name}:0",
            "cost_micro_usd": 1000}


def result(run_row, task_row, *, acceptance=True, trajectory=None, case_id="fixture"):
    return {"run_id": run_row["id"], "task_id": task_row["id"], "case_id": case_id, "repeat": 0,
            "acceptance_passed": acceptance, "trajectory_passed": trajectory, "cost_usd": .001,
            "duration_s": 2, "tokens": 100}


def history(status, **changes):
    return {"evolution": {"id": uuid4(), "status": status, "spent_usd": .05,
                          "created_at": NOW, "finished_at": None, "config": {"model": "fake"}},
            "candidates": [], "runs": [], "results": [], "tasks": [], "champions": [], **changes}


def assert_dag(dag):
    parents = defaultdict(set)
    ids = {node.id for node in dag.nodes}
    assert len(ids) == len(dag.nodes)
    for edge in dag.edges:
        assert {edge.source, edge.target} <= ids
        parents[edge.target].add(edge.source)
    remaining = ids.copy()
    while remaining:
        ready = {node for node in remaining if not parents[node] & remaining}
        assert ready
        if remaining == ids:
            assert ready == {dag.nodes[0].id}
        remaining -= ready


def test_complete_generation_paths_with_baseline_and_champion():
    base, first, second, third = [run(i) for i in (1, 3, 5, 10)]
    hold = run(8, "holdout")
    chosen = candidate(2, 1, run_id=first["id"], decision="promoted")
    chosen["holdout_run_id"] = hold["id"]
    chosen["dev_metrics"] = {"pass_rate": 1, "cost_usd": .001}
    rejected = candidate(4, 1, run_id=second["id"], decision="rejected")
    invalid = candidate(6, 1, decision="invalid")
    later = candidate(9, 2, run_id=third["id"], decision="rejected")
    comparison = run(7, "holdout")
    champion = {"id": uuid4(), "candidate_id": chosen["id"], "created_at": NOW + timedelta(seconds=9),
                "metrics": {"dev": chosen["dev_metrics"]}}
    base_task, winner_task, hold_task = (task(row) for row in (base, first, hold))
    data = history("completed", candidates=[later, invalid, rejected, chosen],
                   runs=[third, hold, comparison, second, first, base], champions=[champion],
                   tasks=[base_task, winner_task, hold_task],
                   results=[result(base, base_task, trajectory=False), result(first, winner_task),
                            result(hold, hold_task)])
    data["evolution"]["best_champion_id"] = champion["id"]
    dag = build_evolution_dag(data)
    assert_dag(dag)
    nodes = {node.id: node for node in dag.nodes}
    edges = {(edge.source, edge.target) for edge in dag.edges}
    baseline_id, decision_id = f"eval:{base['id']}", "promotion:1"
    assert (dag.nodes[0].id, baseline_id) in edges
    assert nodes[baseline_id].meta["role"] == "baseline" and nodes[baseline_id].score == 0
    case_id = f"case:{base['id']}:{base_task['id']}"
    assert (baseline_id, case_id) in edges and (case_id, "generation:1") in edges
    assert nodes[case_id].status == "failed" and nodes[case_id].task_id == base_task["id"]
    chosen_id = f"candidate:{chosen['id']}"
    assert ("generation:1", chosen_id) in edges and (chosen_id, f"eval:{first['id']}") in edges
    assert (f"case:{first['id']}:{winner_task['id']}", f"eval:{hold['id']}") in edges
    assert (f"case:{hold['id']}:{hold_task['id']}", decision_id) in edges
    assert ("generation:1", f"eval:{comparison['id']}") in edges
    assert nodes[f"eval:{comparison['id']}"].meta["role"] == "comparison"
    assert nodes[chosen_id].score == 1 and nodes[chosen_id].cost_usd == .002
    assert nodes[f"candidate:{invalid['id']}"].status == "failed"
    champion_id = f"champion:{champion['id']}"
    assert (decision_id, champion_id) in edges and (champion_id, "generation:2") in edges
    assert dag.nodes[0].score == 1 and dag.nodes[0].cost_usd == .05
    assert nodes["promotion:2"].meta["decision"] == "rejected"
    assert dag == build_evolution_dag(data)


def test_live_case_links_and_status_are_preserved_before_collection():
    base = run(1)
    proposal = candidate(2, 1)
    dev = run(3, overlay=proposal["overlay"], status="running")
    live = task(dev, state="RUNNING", name="suite:boundary")
    queued = task(dev, state="QUEUED")
    provider_error = task(dev, state="FAILED")
    provider_error["model_provider_error"] = {"kind": "billing"}
    data = history("running", candidates=[proposal], runs=[dev, base], tasks=[queued, live, provider_error],
                   results=[result(dev, provider_error, acceptance=None, case_id="provider")])
    dag = build_evolution_dag(data)
    assert_dag(dag)
    nodes = {node.id: node for node in dag.nodes}
    eval_id = f"eval:{dev['id']}"
    assert any(edge.source == f"candidate:{proposal['id']}" and edge.target == eval_id for edge in dag.edges)
    assert nodes[eval_id].status == "running" and nodes[eval_id].score is None
    assert nodes[eval_id].cost_usd == .003
    live_case = nodes[f"case:{dev['id']}:{live['id']}"]
    assert live_case.meta["parent"] == eval_id and live_case.status == "running"
    assert live_case.meta["case_id"] == "suite:boundary" and live_case.meta["repeat"] == 0
    assert nodes[f"case:{dev['id']}:{queued['id']}"].status == "queued"
    assert nodes["promotion:1"].status == "queued"

    dev["status"] = "completed"
    proposal.update(dev_run_id=dev["id"], reason="awaiting approval")
    data["evolution"]["status"] = "awaiting_approval"
    hold = run(4, "holdout", proposal["overlay"], status="completed")
    data["runs"].append(hold)
    updated = build_evolution_dag(data)
    assert_dag(updated)
    assert any(edge.source == live_case.id and edge.target == f"eval:{hold['id']}" for edge in updated.edges)
    assert next(node for node in updated.nodes if node.id == "promotion:1").status == "running"


def test_reproposal_stops_old_candidates_in_the_same_generation_band():
    from sca.eval.errors import GUIDANCE_ABORT

    old = {**candidate(1, 1, decision="rejected"), "reason": GUIDANCE_ABORT}
    data = history("running", candidates=[old])
    dag = build_evolution_dag(data)
    nodes = {node.id: node for node in dag.nodes}
    stopped = nodes[f"candidate:{old['id']}"]
    assert stopped.status == "stopped" and GUIDANCE_ABORT in stopped.label
    assert nodes["generation:1"].status == "running"
    new = candidate(2, 1)
    data["candidates"].append(new)
    dag = build_evolution_dag(data)
    assert {edge.target for edge in dag.edges if edge.source == "generation:1"} == {
        stopped.id, f"candidate:{new['id']}",
    }


def test_partial_eval_scores_are_hidden_and_live_spend_counts_each_case_once():
    base = run(1, status="running")
    first, second = task(base), task(base, state="RUNNING", name="second")
    data = history("running", runs=[base], tasks=[first, second],
                   results=[result(base, first)], proposer_spent_usd=.002)
    data["evolution"]["spent_usd"] = 0
    first["cost_micro_usd"] = 2000
    root, evaluation, *_ = build_evolution_dag(data).nodes
    assert root.score is evaluation.score is None
    assert evaluation.meta["cases_passed"] == 1
    assert root.cost_usd == pytest.approx(.004)
    assert root.meta["eval_spent_usd"] == pytest.approx(.002)

    second["state"] = "FAILED"
    data["results"].append(result(base, second, acceptance=False, case_id="second"))
    base["status"] = "completed"
    root, evaluation, *_ = build_evolution_dag(data).nodes
    assert root.score is None and evaluation.score == .5
    data["evolution"].update(status="completed", spent_usd=.002)
    root = build_evolution_dag(data).nodes[0]
    assert root.score == .5 and root.cost_usd == pytest.approx(.004)


def test_generation_score_excludes_partial_candidate_runs():
    valid, partial = candidate(1, 1, decision="rejected"), candidate(2, 1)
    completed = run(3, overlay=valid["overlay"])
    running = run(4, overlay=partial["overlay"], status="running")
    valid.update(dev_run_id=completed["id"], dev_metrics={"pass_rate": 0})
    partial.update(dev_run_id=running["id"], dev_metrics={"pass_rate": 1})
    first, second = task(completed), task(running)
    data = history("running", candidates=[valid, partial], runs=[completed, running], tasks=[first, second],
                   results=[result(completed, first, acceptance=False), result(running, second)])
    nodes = {node.id: node for node in build_evolution_dag(data).nodes}
    generation = nodes["generation:1"]
    assert generation.status == "running" and generation.score is None
    assert nodes[f"candidate:{partial['id']}"].score is None

    running["status"] = "budget_exhausted"
    partial["decision"] = "rejected"
    nodes = {node.id: node for node in build_evolution_dag(data).nodes}
    generation = nodes["generation:1"]
    assert generation.status == "succeeded" and generation.score == 0


@pytest.mark.parametrize(("stored", "expected"), [
    ("queued", "queued"), ("running", "running"), ("completed", "succeeded"), ("failed", "failed"),
    ("stopped", "stopped"), ("budget_exhausted", "stopped"), ("patience_exhausted", "stopped"),
])
def test_evolution_status_before_any_eval(stored, expected):
    dag = build_evolution_dag(history(stored))
    root, = dag.nodes
    assert root.status == expected
    assert dag.edges == []


def test_incomplete_history_keeps_unassigned_run_and_omits_missing_task():
    proposal = candidate(2, 1, run_id=uuid4())
    old_run = run(3, status="failed")
    missing = task(old_run)
    dag = build_evolution_dag(history("failed", candidates=[proposal], runs=[old_run],
                                      results=[result(old_run, missing)]))
    assert_dag(dag)
    nodes = {node.id: node for node in dag.nodes}
    assert nodes[f"eval:{old_run['id']}"].meta["role"] == "unassigned"
    assert nodes["generation:1"].status == "failed"
    assert not any(node.kind == "case_task" for node in dag.nodes)


async def test_demo_seeds_two_generations_and_task_history():
    rows = defaultdict(list)

    class DemoDatabase:
        @asynccontextmanager
        async def begin(self):
            yield self

        async def execute(self, statement, values=None):
            if isinstance(statement, Insert):
                rows[statement.table.name].extend(values or [statement.compile(dialect=postgresql.dialect()).params])
            elif isinstance(statement, Update):
                rows["evolutions"][0]["spent_usd"] = statement.compile().params["spent_usd"]

    evolution_id = await seed_demo(DemoDatabase())
    assert rows["evolutions"][0]["id"] == evolution_id
    assert all(row["spec"]["agent_backend"] == "fake" for row in rows["tasks"])
    assert len(rows["tasks"]) == len(rows["eval_results"]) == 18
    assert {row["task_id"] for row in rows["events"] if row["type"] == "agent.turn_completed"} == {
        row["id"] for row in rows["tasks"]
    }
    dag = build_evolution_dag(history("completed", evolution=rows["evolutions"][0],
                                      candidates=rows["evolution_candidates"], runs=rows["eval_runs"],
                                      results=rows["eval_results"], tasks=rows["tasks"], champions=rows["champions"]))
    assert_dag(dag)
    assert sum(node.kind == "generation" for node in dag.nodes) == 2
    assert sum(node.kind == "candidate" for node in dag.nodes) == 6
    assert [node.meta["decision"] for node in dag.nodes if node.kind == "promotion"] == ["promoted", "rejected"]
    assert sum(node.cost_usd for node in dag.nodes if node.kind == "eval_run") == pytest.approx(dag.nodes[0].cost_usd)
