import socket
from types import SimpleNamespace

import httpx
import pytest

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings
from sca.core.models import TaskState
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration


def postgres_socket_denied():
    try:
        with socket.socket(socket.AF_INET):
            return False
    except PermissionError:
        return True


@pytest.mark.skipif(postgres_socket_denied(), reason="Sandbox denies PostgreSQL sockets")
async def test_portal_quota_with_real_admission_and_usage(store, api_store, tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path, tenant_max_queued=7)
    monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)
    for tenant in ("customer", "other"):
        await store.create_tenant(tenant, tenant, "free", None)

    async def task(tenant):
        row, _ = await store.create_task(tenant_id=tenant, kind="coding",
                                         spec={"instruction": "fixture"}, idempotency_key=None)
        return row.id

    finished = await task("customer")
    await store.add_usage(finished, {"cost_micro_usd": 250_000})
    await store.set_state(finished, TaskState.CANCELLED)
    active = await task("customer")
    assert (await store.acquire_slot(str(active), 4))["granted"]
    await store.set_state(active, TaskState.PROVISIONING)
    await task("customer")
    await task("other")

    app = create_app(store=api_store, orchestrator=SimpleNamespace(),
                     authenticator=StaticTokenAuthenticator({"fixture-team": "customer:admin"}),
                     object_store=LocalObjectStore(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get("/v1/portal/tenant-quota",
                                  headers={"Authorization": "Bearer fixture-team"})
    assert result.status_code == 200
    assert result.json() == {
        "monthly_cost_usd": .25, "monthly_cost_limit_usd": 10, "max_task_cost_usd": 1,
        "slots_used": 1, "slots_limit": 1, "queued": 1, "queued_limit": 7,
    }
