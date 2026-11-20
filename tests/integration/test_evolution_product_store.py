"""Phase 6 storage, migration and RLS with the restricted API role."""

import socket
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from sca.config import get_settings
from sca.core.errors import ConflictError, NotFoundError
from sca.core.models import AgentOverlay
from sca.db.schema import (
    champion_rollouts,
    evolution_events,
    evolution_schedules,
    harvest_candidates,
)
from sca.evolve.models import EvolutionScheduleSpec

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def postgres_available():
    url = make_url(get_settings().database_url)
    try:
        with socket.create_connection((url.host or "localhost", url.port or 5432), timeout=2):
            pass
    except OSError as exc:
        pytest.skip(f"Postgres unavailable: {type(exc).__name__}")


async def test_rollout_rollback_updates_tenants_and_audit_but_keeps_task_pin(api_store):
    platform, team, other = (api_store.for_tenant(tenant) for tenant in ("eval", "team", "other"))
    parent, child, unaffected = uuid4(), uuid4(), uuid4()
    values = {"agent_version": "v", "model": "m", "overlay": {"tool_guidance": "base"},
              "overlay_sha256": AgentOverlay(tool_guidance="base").sha256(), "metrics": {}}
    await platform.activate_champion(str(parent), values)
    child_values = {**values, "parent_champion_id": parent}
    await platform.activate_champion(str(child), child_values, rollout=(10, "scheduler"))
    await platform.activate_champion(str(unaffected), {**values, "model": "other"})
    await team.put_champion_rollout("v", "m", child, 75, "team-admin")
    await team.put_champion_rollout("v", "m", child, 75, "retry")
    await other.put_champion_rollout("v", "other", unaffected, 100, "other-admin")
    with pytest.raises(NotFoundError):
        await team.put_champion_rollout("v", "wrong-model", child, 50, "team-admin")
    task, _ = await team.create_task(tenant_id="team", kind="coding", spec={}, idempotency_key=None)
    selection = {"champion_id": str(child), "overlay_sha256": values["overlay_sha256"]}
    await team.pin_task_champion(str(task.id), selection)
    await team.pin_task_champion(str(task.id), {"champion_id": str(parent)})

    with pytest.raises(DBAPIError):
        async with team._transaction() as session:
            await session.execute(text("SELECT retarget_champion_rollouts(:id, 'team-admin')"), {"id": child})
    assert (await platform.rollback_champion(child, "platform-admin"))["id"] == parent
    assert (await platform.get_champion_rollout("v", "m"))["champion_id"] == parent
    rollout, = await team.list_champion_rollouts()
    assert (rollout["champion_id"], rollout["percent"], rollout["updated_by"]) == (parent, 75, "platform-admin")
    assert (await other.get_champion_rollout("v", "other"))["champion_id"] == unaffected
    assert (await platform.get_active_champion("v", "m"))["id"] == parent
    await platform.activate_champion(str(child), child_values, rollout=(10, "scheduler"))
    assert (await platform.get_active_champion("v", "m"))["id"] == parent
    assert (await team.get_task_champion(str(task.id)))["id"] == child
    applied, = [e for e in await team.list_events(task.id) if e.type == "champion.applied"]
    assert applied.payload == selection
    assert (await team.get_task(task.id)).result["overlay_sha256"] == values["overlay_sha256"]
    async with team._transaction() as session:
        rollouts = (await session.execute(select(champion_rollouts))).mappings().all()
        assert [row["tenant_id"] for row in rollouts] == ["team"]
        audit = (await session.execute(select(evolution_events).order_by(evolution_events.c.id))).mappings().all()
        assert [(row["tenant_id"], row["type"]) for row in audit] == [
            ("team", "rollout.updated"), ("team", "rollout.rolled_back"),
        ]
        assert audit[-1]["payload"]["previous_champion_id"] == str(child)
    with pytest.raises(ConflictError):
        await platform.rollback_champion(parent, "platform-admin")


async def test_schedule_crud_due_and_outcome_are_tenant_scoped(api_store):
    platform, other = api_store.for_tenant("eval"), api_store.for_tenant("other")
    values = EvolutionScheduleSpec(suite="suite", agent_version="v", interval_hours=6, threshold=.8).model_dump()
    schedule = await platform.create_evolution_schedule(values)
    schedule_id = schedule["id"]
    now = datetime.now(UTC)
    assert await platform.due_evolution_schedules(now) == [schedule_id]
    assert await other.list_evolution_schedules() == []
    assert await other.claim_evolution_schedule(schedule_id, now, True) is None
    with pytest.raises(NotFoundError):
        await other.get_evolution_schedule(schedule_id)
    async with other._transaction() as session:
        assert not (await session.execute(select(evolution_schedules))).all()
    assert await platform.claim_evolution_schedule(schedule_id, now, False)
    assert await platform.claim_evolution_schedule(schedule_id, now, False)
    assert await platform.due_evolution_schedules(now + timedelta(hours=5)) == []
    assert await platform.due_evolution_schedules(now + timedelta(hours=6)) == [schedule_id]
    eval_id, evolution_id = uuid4(), uuid4()
    await platform.create_eval_run({"id": eval_id, "tenant_id": "eval", "suite": "suite",
                                    "suite_hash": "sha", "variant": {}, "status": "completed",
                                    "created_by": "test"})
    evolution = {"id": evolution_id, "status": "queued", "config": {}, "created_by": "test"}
    assert await platform.record_evolution_schedule_run(schedule_id, eval_id, evolution)
    row = await platform.update_evolution_schedule(schedule_id, {"enabled": False})
    assert row["last_run_at"] == now
    assert row["last_eval_run_id"] == eval_id
    assert row["last_evolution_id"] == evolution_id
    assert (await platform.get_evolution(str(evolution_id)))["status"] == "queued"
    assert await platform.due_evolution_schedules(now + timedelta(hours=7)) == []
    assert await platform.claim_evolution_schedule(schedule_id, now + timedelta(hours=1), True)
    await platform.delete_evolution_schedule(schedule_id)
    assert not await platform.list_evolution_schedules()


async def test_harvest_review_and_rls(api_store):
    owner, other = api_store.for_tenant("team"), api_store.for_tenant("other")
    task, _ = await owner.create_task(tenant_id="team", kind="coding", spec={}, idempotency_key=None)
    candidate_id = uuid4()
    values = {"id": candidate_id, "task_id": task.id, "suite": "opencode-harvested",
              "split": "holdout", "case_id": f"harvest-{candidate_id.hex}", "base_sha": "git-sha",
              "notes": "", "created_by": "submitter"}
    assert (await owner.create_harvest_candidate(values))["status"] == "pending"
    with pytest.raises(NotFoundError):
        await other.create_harvest_candidate({**values, "id": uuid4()})
    assert await other.list_harvest_candidates() == []
    async with other._transaction() as session:
        assert not (await session.execute(select(harvest_candidates))).all()
    with pytest.raises(DBAPIError):
        async with other._transaction() as session:
            await session.execute(insert(harvest_candidates).values(
                **{**values, "id": uuid4()}, tenant_id="team"))
    published = []

    async def fail(_row):
        raise OSError("fixture")

    async def publish(row):
        published.append(row["id"])

    with pytest.raises(OSError):
        await owner.review_harvest_candidate(candidate_id, "approved", "reviewer", fail)
    assert (await owner.list_harvest_candidates())[0]["status"] == "pending"
    with pytest.raises(NotFoundError):
        await other.review_harvest_candidate(candidate_id, "rejected", "other")
    approved = await owner.review_harvest_candidate(candidate_id, "approved", "reviewer", publish)
    assert (approved["status"], approved["reviewed_by"]) == ("approved", "reviewer")
    await owner.review_harvest_candidate(candidate_id, "approved", "retry", publish)
    assert published == [candidate_id]
    with pytest.raises(ConflictError):
        await owner.review_harvest_candidate(candidate_id, "rejected", "reviewer")
    second = uuid4()
    await owner.create_harvest_candidate({**values, "id": second, "case_id": f"harvest-{second.hex}"})
    assert (await owner.review_harvest_candidate(second, "rejected", "reviewer"))["status"] == "rejected"
