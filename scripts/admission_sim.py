"""Postgres-only admission load simulation in an isolated temporary database."""

import asyncio
import importlib
import json
import statistics
from collections import Counter
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from sca.admission.order import grant_order
from sca.config import get_settings
from sca.core.models import TaskState
from sca.db.engine import make_sessionmaker
from sca.db.repo import Store


async def main() -> None:
    database = "test_admission_sim_" + uuid4().hex
    url = make_url(get_settings().database_url)
    admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
    engine = create_async_engine(url.set(database=database), pool_size=4, max_overflow=0)
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{database}"'))
    try:
        def migrate(conn):
            with Operations.context(MigrationContext.configure(conn)):
                for file in sorted(Path("src/sca/db/migrations/versions").glob("[0-9]*.py")):
                    importlib.import_module("sca.db.migrations.versions." + file.stem).upgrade()

        async with engine.begin() as conn:
            await conn.run_sync(migrate)
        store = Store(make_sessionmaker(engine))
        ids = []
        counts = (200, 70, 30)
        for tenant, count in enumerate(counts):
            await store.set_tenant_quota(str(tenant), 3)
            for index in range(count):
                task, _ = await store.create_task(
                    tenant_id=str(tenant), kind="coding",
                    spec={"priority": ("interactive", "eval", "batch")[index % 3]},
                    idempotency_key=None)
                ids.append({"id": str(task.id), "tenant_id": str(tenant),
                            "spec": task.spec, "slot_requested_at": task.created_at})
        waits = {str(i): [] for i in range(3)}
        grants = []

        held = Counter()
        active = set()
        weights = {str(i): 1 for i in range(3)}

        async def work(task_id: str, tenant: str) -> None:
            try:
                await asyncio.sleep(0.02)
                await store.set_state(task_id, TaskState.CANCELLED)
            finally:
                held[tenant] -= 1

        async with asyncio.timeout(240):
            while ids or active:
                completed = {job for job in active if job.done()}
                for job in completed:
                    await job
                active -= completed
                eligible = [row for row in ids if held[row["tenant_id"]] < 3]
                if sum(held.values()) < 4 and eligible:
                    row = min(eligible, key=lambda r: grant_order(r, held, weights))
                    result = await store.acquire_slot(row["id"], 4)
                    if result["granted"]:
                        tenant = row["tenant_id"]
                        held[tenant] += 1
                        waits[tenant].append(result["wait_seconds"])
                        grants.append(tenant)
                        ids.remove(row)
                        active.add(asyncio.create_task(work(row["id"], tenant)))
                        continue
                await asyncio.sleep(0.01)
        result = {}
        for tenant, values in waits.items():
            values.sort()
            result[tenant] = {
                "grants": len(values), "share": len(values) / len(grants),
                "wait_p50_s": round(statistics.median(values), 3),
                "wait_p90_s": round(values[int((len(values) - 1) * .9)], 3),
                "first_30_grants": grants[:30].count(tenant),
            }
        print(json.dumps(result, indent=2))
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        await admin.dispose()


if __name__ == "__main__":
    asyncio.run(main())
