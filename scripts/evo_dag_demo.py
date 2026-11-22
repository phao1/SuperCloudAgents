#!/usr/bin/env python3
"""Seed a sample evolution and fake-backend task history in a migrated demo database."""

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import insert, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sca.config import Settings
from sca.db.engine import make_engine
from sca.db.schema import (
    champions,
    eval_results,
    eval_runs,
    events,
    evolution_candidates,
    evolutions,
    tasks,
)
from sca.evolve.checks import overlay_hash

TENANT = "eval"
SUITE = "evolution-dag-demo"
VERSION = "fake-evolution-dag"
ACTOR = "evo-dag-demo"


async def seed_eval(
    conn: AsyncConnection, evolution_id: UUID, split: str, overlay: dict,
    at: datetime, outcomes: tuple[bool, bool], cost: float,
) -> tuple[UUID, dict]:
    run_id = uuid4()
    await conn.execute(insert(eval_runs).values(
        id=run_id, tenant_id=TENANT, suite=SUITE, suite_hash=SUITE, created_by=ACTOR,
        created_at=at, finished_at=at + timedelta(seconds=5), status="completed",
        variant={"id": str(run_id), "suite": SUITE, "evolution_id": str(evolution_id), "split": split,
                 "repeat": 1, "variant": {"agent_backend": "fake", "agent_version": VERSION,
                                            "model": "fake", "agent_overlay": overlay}},
    ))
    for case, passed in zip((f"{split}-parse", f"{split}-validate"), outcomes, strict=True):
        task_id = uuid4()
        state = "SUCCEEDED" if passed else "FAILED"
        spec = {"agent_backend": "fake", "model": "fake", "eval_run_id": str(run_id),
                "instruction": f"Check the {case} fixture.\n\n```sca-fake\nprintf 'fixture\\n'\n```"}
        await conn.execute(insert(tasks).values(
            id=task_id, tenant_id=TENANT, kind="coding", state=state, spec=spec,
            result={"acceptance_passed": passed, "verification_passed": passed, "turns": 1},
            usage={"cost_micro_usd": round(cost * 1e6), "input_tokens": 40, "output_tokens": 20},
            idempotency_key=f"eval:{run_id}:{case}:0", created_at=at, updated_at=at + timedelta(seconds=4),
        ))
        trace = [
            ("task.created", {"actor": ACTOR, "spec": spec}),
            ("agent.turn_started", {"turn": 1, "backend": "fake"}),
            ("agent.turn_completed", {"turn": 1, "cost_usd": cost}),
            ("acceptance.completed", {"turn": 1, "passed": passed}),
            ("task.state_changed", {"from": "VERIFYING", "to": state}),
        ]
        await conn.execute(insert(events), [
            {"task_id": task_id, "tenant_id": TENANT, "seq": i + 1, "type": kind,
             "payload": payload, "created_at": at + timedelta(seconds=i)}
            for i, (kind, payload) in enumerate(trace)
        ])
        await conn.execute(insert(eval_results).values(
            run_id=run_id, tenant_id=TENANT, case_id=case, repeat=0, task_id=task_id,
            acceptance_passed=passed, verification_passed=passed, turns=1, tokens=60,
            cost_usd=cost, duration_s=4,
        ))
    return run_id, {"pass_rate": sum(outcomes) / 2, "cost_usd": cost * 2, "turns": 1}


async def seed_demo(engine: AsyncEngine) -> UUID:
    evolution_id, champion_id = uuid4(), uuid4()
    at = datetime.now(UTC) - timedelta(minutes=3)
    async with engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": TENANT})
        await conn.execute(insert(evolutions).values(
            id=evolution_id, tenant_id=TENANT, status="completed", created_by=ACTOR,
            config={"suite": SUITE, "agent_version": VERSION, "model": "fake", "generations": 2,
                    "candidates": 3, "budget_usd": .1},
            created_at=at, updated_at=at + timedelta(seconds=120), finished_at=at + timedelta(seconds=120),
            best_champion_id=champion_id,
        ))
        _, baseline = await seed_eval(conn, evolution_id, "dev", {}, at + timedelta(seconds=5), (True, False), .001)
        spent = baseline["cost_usd"]
        proposals = [
            (1, 15, "Check empty input before parsing", "promoted", (True, True), .0012, (60, (True, True))),
            (1, 28, "Verify one sample before editing", "rejected", (True, False), .001, None),
            (1, 40, "Use a shorter checklist", "invalid", None, 0, None),
            (2, 75, "Reuse successful validation output", "rejected", (True, True), .0005, (110, (True, False))),
            (2, 87, "Split validation into two steps", "rejected", (True, False), .0012, None),
            (2, 99, "List boundary cases before editing", "rejected", (True, True), .0015, None),
        ]
        winner = None
        for number, offset, hypothesis, decision, outcomes, cost, holdout in proposals:
            parent = winner if number == 2 else None
            start = at + timedelta(seconds=offset)
            candidate_id, overlay = uuid4(), {"tool_guidance": hypothesis}
            scores = {}
            if outcomes:
                dev_id, dev = await seed_eval(conn, evolution_id, "dev", overlay,
                                              start + timedelta(seconds=1), outcomes, cost)
                scores.update(dev_run_id=dev_id, dev_metrics=dev)
                spent += dev["cost_usd"]
            if decision == "promoted":
                winner = candidate_id
                _, comparison = await seed_eval(conn, evolution_id, "holdout", {}, at + timedelta(seconds=53),
                                                (True, False), .001)
                spent += comparison["cost_usd"]
            if holdout:
                start_holdout, holdout_outcomes = holdout
                hold_id, hold = await seed_eval(conn, evolution_id, "holdout", overlay,
                                               at + timedelta(seconds=start_holdout), holdout_outcomes, cost)
                scores.update(holdout_run_id=hold_id, holdout_metrics=hold)
                spent += hold["cost_usd"]
            await conn.execute(insert(evolution_candidates).values(
                id=candidate_id, tenant_id=TENANT, evolution_id=evolution_id, generation=number,
                parent_id=parent, overlay=overlay, overlay_sha256=overlay_hash(overlay), hypothesis=hypothesis,
                diagnosis={}, static_check={"valid": decision != "invalid"}, decision=decision, created_at=start,
                **scores,
            ))
            if decision == "promoted":
                await conn.execute(insert(champions).values(
                    id=champion_id, tenant_id=TENANT, agent_version=VERSION, model="fake",
                    candidate_id=candidate_id, overlay=overlay, overlay_sha256=overlay_hash(overlay),
                    metrics={"dev": dev, "holdout": hold}, created_at=at + timedelta(seconds=65),
                ))
        await conn.execute(update(evolutions).where(
            evolutions.c.id == evolution_id, evolutions.c.tenant_id == TENANT,
        ).values(spent_usd=round(spent, 6)))
    return evolution_id


async def run(url: str) -> None:
    engine = make_engine(Settings(_env_file=None).database_url)
    try:
        evolution_id = await seed_demo(engine)
    finally:
        await engine.dispose()
    print(f"{url.rstrip('/')}/portal#/evolutions/runs/{evolution_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="Portal base URL")
    asyncio.run(run(parser.parse_args().url))


if __name__ == "__main__":
    main()
