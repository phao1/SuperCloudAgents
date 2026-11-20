"""SCM migration, tenant isolation and atomic task links on Postgres."""

import socket
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from sca.config import get_settings
from sca.core.errors import ConflictError, NotFoundError
from sca.db.schema import scm_integrations

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def postgres_available():
    url = make_url(get_settings().database_url)
    try:
        with socket.create_connection((url.host or "localhost", url.port or 5432), timeout=2):
            pass
    except OSError as exc:
        pytest.skip(f"Postgres unavailable: {type(exc).__name__}")


_INTEGRATION = {
    "provider": "gitea", "host": "forge.test:3000", "repo_full_name": "acme/project",
    "webhook_secret_ref": "repo-hook", "default_spec": {"verify_command": "pytest -q"},
}


async def test_scm_integration_crud_and_tenant_rls(api_store):
    alpha = api_store.for_tenant("alpha")
    beta = api_store.for_tenant("beta")
    first = await alpha.create_scm_integration(_INTEGRATION)
    second = await beta.create_scm_integration(_INTEGRATION)
    assert [row["id"] for row in await alpha.list_scm_integrations()] == [first["id"]]
    assert [row["id"] for row in await beta.list_scm_integrations()] == [second["id"]]
    assert (await alpha.get_scm_integration(first["id"]))["default_spec"] == _INTEGRATION["default_spec"]
    with pytest.raises(ConflictError):
        await alpha.create_scm_integration(_INTEGRATION)
    with pytest.raises(ConflictError):
        await alpha.create_scm_integration({**_INTEGRATION, "repo_full_name": "acme/other"})
    with pytest.raises(NotFoundError):
        await beta.get_scm_integration(first["id"])
    with pytest.raises(NotFoundError):
        await beta.delete_scm_integration(first["id"])

    async with beta._transaction() as session:
        assert (await session.scalars(select(scm_integrations.c.id))).all() == [second["id"]]
        stmt = update(scm_integrations).where(scm_integrations.c.id == first["id"]).values(enabled=False)
        assert (await session.execute(stmt)).rowcount == 0
        stmt = delete(scm_integrations).where(scm_integrations.c.id == first["id"])
        assert (await session.execute(stmt)).rowcount == 0
    with pytest.raises(DBAPIError) as rejected:
        async with beta._transaction() as session:
            values = {**_INTEGRATION, "repo_full_name": "acme/new", "webhook_secret_ref": "new-hook"}
            await session.execute(insert(scm_integrations).values(
                **values, id=uuid4(), tenant_id="alpha",
            ))
    assert rejected.value.orig.sqlstate == "42501"
    async with api_store._transaction() as session:
        assert (await session.scalars(select(scm_integrations.c.id))).all() == []

    args = ("gitea", "forge.test:3000", "acme/project", "repo-hook")
    assert (await alpha.find_scm_integration(*args))["id"] == first["id"]
    assert await alpha.find_scm_integration(*args[:3], "wrong-ref") is None
    disabled = await alpha.replace_scm_integration(first["id"], {**_INTEGRATION, "enabled": False})
    assert not disabled["enabled"] and await alpha.find_scm_integration(*args) is None
    await alpha.delete_scm_integration(first["id"])
    assert await alpha.list_scm_integrations() == []
    assert (await beta.get_scm_integration(second["id"]))["enabled"]


async def test_issue_task_persists_issue_and_deduplicates_link_and_pr_event(api_store):
    store = api_store.for_tenant("alpha")
    issue = {"provider": "github", "number": 21, "url": "https://github.com/acme/project/issues/21"}
    values = {
        "tenant_id": "alpha", "kind": "coding", "spec": {}, "idempotency_key": "scm:issue:21",
        "initial_result": {"issue": issue},
    }
    task, created = await store.create_task(**values)
    duplicate, recreated = await store.create_task(**values)
    assert created and not recreated and task.id == duplicate.id and duplicate.result["issue"] == issue
    pr = {"provider": "github", "number": 22, "url": "https://github.com/acme/project/pull/22",
          "state": "open"}
    for _ in range(2):
        assert await store.record_scm_pull_request(task.id, pr, opened=True) == pr
    await store.append_event(task.id, "verification.completed", {"turn": 1, "passed": False})
    await store.append_event(task.id, "verification.completed", {"turn": 2, "passed": True})
    assert await store.scm_check_results(task.id, 2) == {
        "verification.completed": {"turn": 2, "passed": True},
    }
    merged = {**pr, "state": "merged"}
    await store.record_scm_pull_request(task.id, merged)
    assert await store.record_scm_pull_request(task.id, pr) == merged
    assert (await store.get_task(task.id)).result == {"issue": issue, "pr": merged}
    assert [event.payload for event in await store.list_events(task.id) if event.type == "pr.opened"] == [pr]
