from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import DBAPIError

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.core.errors import NotFoundError
from sca.core.models import TaskState
from sca.db.schema import memories, memory_events
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration


async def task(store, tenant="one", repo="https://EXAMPLE.com/project/"):
    return (
        await store.create_task(
            tenant_id=tenant,
            kind="coding",
            spec={"repo_url": repo, "instruction": "fix"},
            idempotency_key=None,
        )
    )[0]


async def test_scopes_caps_and_audit(store, api_store):
    t = await task(store)
    tenant = api_store.for_tenant("one")
    active = await tenant.create_memory(
        {
            "text": "Use pnpm",
            "scope": "repo:https://example.COM/project",
            "category": "command",
        },
        "alice",
    )
    await tenant.create_memory({"text": "Tenant convention", "scope": "tenant"}, "alice")
    await tenant.create_memory({"text": "Kind convention", "scope": "kind:coding"}, "alice")
    await tenant.create_memory({"text": "Unrelated", "scope": "repo:https://other/repo"}, "alice")
    proposed = await tenant.propose_memories(
        str(t.id), "agent_proposal", [{"text": "Not approved"}]
    )
    assert proposed == await tenant.propose_memories(
        str(t.id), "agent_proposal", [{"text": "Retry ignored"}]
    )
    content = await tenant.inject_memories(str(t.id), 30, 3000)
    assert all(text in content for text in ("Use pnpm", "Tenant convention", "Kind convention"))
    assert "Unrelated" not in content and "Not approved" not in content
    assert await tenant.inject_memories(str(t.id), 0, 0) == content
    rows = await tenant.list_memories()
    assert next(r for r in rows if r["id"] == active["id"])["uses"] == 1
    injected = [e for e in await store.list_events(t.id) if e.type == "memory.injected"]
    assert len(injected) == 1 and len(injected[0].payload["ids"]) == 3
    t2 = await task(store)
    limited = await tenant.inject_memories(str(t2.id), 1, 170)
    assert len(limited) <= 170 and limited.count("\n- ") == 1
    t3 = await task(store)
    assert await tenant.inject_memories(str(t3.id), 30, 5) == ""
    await tenant.change_memory(UUID(proposed[0]), "alice", {"status": "rejected"})
    await tenant.change_memory(active["id"], "alice", {"status": "retired"})
    await tenant.delete_memory(active["id"], "alice")
    async with store._transaction() as session:
        actions = (
            await session.scalars(
                select(memory_events.c.action).where(memory_events.c.memory_id == active["id"])
            )
        ).all()
    assert actions == ["create", "retire", "delete"]


async def test_rls_and_application_isolation(store, api_store):
    one, two = api_store.for_tenant("one"), api_store.for_tenant("two")
    row = await one.create_memory({"scope": "tenant", "text": "private"}, "alice")
    assert await two.list_memories() == []
    with pytest.raises(NotFoundError):
        await two.change_memory(row["id"], "mallory", {"status": "retired"})
    with pytest.raises(NotFoundError):
        await two.delete_memory(row["id"], "mallory")
    async with two._transaction() as session:
        assert (await session.scalars(select(memories))).all() == []
        assert (await session.scalars(select(memory_events))).all() == []
    with pytest.raises(DBAPIError):
        async with two._transaction() as session:
            await session.execute(
                insert(memories).values(
                    id=uuid4(),
                    tenant_id="one",
                    scope="tenant",
                    text="injected",
                    category="command",
                    status="active",
                    source="manual",
                    created_by="mallory",
                )
            )
    other = await task(store, "two")
    assert await two.inject_memories(str(other.id), 30, 3000) == ""


async def test_review_accepts_only_own_proposals(store, api_store, tmp_path):
    class Orchestrator:
        async def submit_review(self, *args):
            pass

    t = await task(store)
    tenant = api_store.for_tenant("one")
    ids = await tenant.propose_memories(str(t.id), "agent_proposal", [{"text": "Use pnpm"}])
    for state in (
        TaskState.PROVISIONING,
        TaskState.RUNNING,
        TaskState.VERIFYING,
        TaskState.AWAITING_REVIEW,
    ):
        await store.set_state(t.id, state)
    app = create_app(
        store=api_store,
        orchestrator=Orchestrator(),
        authenticator=StaticTokenAuthenticator({"a": "one", "b": "two"}),
        object_store=LocalObjectStore(tmp_path),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer a"},
    ) as c:
        url = f"/v1/tasks/{t.id}/review"
        assert (
            await c.post(url, json={"decision": "approve", "accept_memories": [str(uuid4())]})
        ).status_code == 422
        r = await c.post(url, json={"decision": "approve", "accept_memories": ids})
        assert r.status_code == 200, r.text
        row = (await tenant.list_memories(status="active"))[0]
        assert row["approved_by"] and row["approved_at"]
        r = await c.patch("/v1/memories/" + ids[0], json={"text": "updated"})
        assert r.status_code == 200, r.text
        assert (await c.get("/v1/memories", headers={"Authorization": "Bearer b"})).json() == []
        r = await c.post("/v1/memories", json={"scope": "tenant", "text": "manual"})
        assert r.status_code == 201 and r.json()["status"] == "active"
        assert (
            await c.patch("/v1/memories/" + ids[0], json={"text": "x" * 501})
        ).status_code == 422
        assert (await c.delete("/v1/memories/" + ids[0])).status_code == 204
    async with tenant._transaction() as session:
        actions = (
            await session.scalars(
                select(memory_events.c.action).where(memory_events.c.memory_id == UUID(ids[0]))
            )
        ).all()
    assert actions == ["create", "approve", "update", "delete"]


async def test_kernel_distillation_and_proposal_read(store, tmp_path):
    import io
    import tarfile

    from sca.config import Settings
    from sca.kernel.activities import KernelActivities, StepInput
    from sca.storage.local import key_for

    class Gateway:
        calls = 0

        async def complete(self, messages, model):
            self.calls += 1
            assert "Use pnpm" in messages[1]["content"]
            assert "final diff" in messages[1]["content"]
            return '[{"text":"Use pnpm","category":"command"}]'

    class Sandbox:
        async def download(self, *args):
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as tar:
                data = b"- [pitfall] Run tests serially\n"
                info = tarfile.TarInfo("memory-proposals.md")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            yield archive.getvalue()

    t = await task(store)
    objects = LocalObjectStore(tmp_path / "objects")
    gateway = Gateway()
    activities = KernelActivities(
        settings=Settings(data_dir=tmp_path),
        store=store,
        sandbox=Sandbox(),
        agent=SimpleNamespace(name="fake"),
        objects=objects,
        gateway=gateway,
    )
    activities._workspace(str(t.id)).workspace.mkdir(parents=True)
    await activities.memory_initial_prompt(str(t.id))
    assert (activities._workspace(str(t.id)).workspace / ".sca/context/MEMORY.md").exists()
    await store.update_result(t.id, {"sandbox_id": "fake", "patch_artifact": "changes.patch"})
    await objects.put(key_for(str(t.id), "changes.patch"), b"final diff")
    await activities.collect_memory_proposals(StepInput(str(t.id), 1))
    await activities.collect_memory_proposals(StepInput(str(t.id), 1))
    assert len(await store.for_tenant("one").list_memories()) == 1
    await activities.distill_memories(str(t.id))
    assert gateway.calls == 0
    await store.append_event(
        t.id,
        "review.submitted",
        {"decision": "request_changes", "feedback": "Use pnpm", "actor": "alice"},
    )
    for state in (
        TaskState.PROVISIONING,
        TaskState.RUNNING,
        TaskState.VERIFYING,
        TaskState.AWAITING_REVIEW,
        TaskState.SUCCEEDED,
    ):
        await store.set_state(t.id, state)
    await activities.distill_memories(str(t.id))
    await activities.distill_memories(str(t.id))
    assert gateway.calls == 1
    rows = await store.for_tenant("one").list_memories()
    assert len(rows) == 2 and all(row["status"] == "proposed" for row in rows)
    assert {row["source"] for row in rows} == {"agent_proposal", "reviewer_feedback"}


@pytest.mark.parametrize("kind", ["coding", "analysis"])
async def test_kernel_context_for_both_scenarios(store, tmp_path, kind):
    from sca.config import Settings
    from sca.kernel.activities import KernelActivities

    tenant = store.for_tenant("one")
    t, _ = await tenant.create_task(tenant_id="one", kind=kind, spec={}, idempotency_key=None)
    await tenant.create_memory(
        {"scope": "kind:" + kind, "text": "Use the kind convention"}, "alice"
    )
    await tenant.create_memory({"scope": "tenant", "text": "Use the tenant convention"}, "alice")
    await tenant.create_memory({"scope": "kind:other", "text": "unrelated"}, "alice")
    acts = KernelActivities(
        settings=Settings(data_dir=tmp_path),
        store=store,
        sandbox=object(),
        agent=SimpleNamespace(name="fake"),
        objects=LocalObjectStore(tmp_path),
    )
    acts._workspace(str(t.id)).workspace.mkdir(parents=True)
    note = await acts.memory_initial_prompt(str(t.id))
    content = (acts._workspace(str(t.id)).workspace / ".sca/context/MEMORY.md").read_text()
    assert "MEMORY.md" in note and "untrusted" in note
    assert "Use the kind convention" in content and "Use the tenant convention" in content
    assert "unrelated" not in content


async def test_memory_write_permissions(api_store, tmp_path):
    app = create_app(
        store=api_store,
        orchestrator=object(),
        authenticator=StaticTokenAuthenticator({"viewer": "one:viewer", "member": "one:member"}),
        object_store=LocalObjectStore(tmp_path),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        row = {"scope": "tenant", "text": "A project convention"}
        response = await client.post(
            "/v1/memories", json=row, headers={"Authorization": "Bearer viewer"}
        )
        assert response.status_code == 403
        response = await client.post(
            "/v1/memories", json=row, headers={"Authorization": "Bearer member"}
        )
        assert response.status_code == 201
        mid = response.json()["id"]
        for method, url in [("patch", "/v1/memories/" + mid), ("delete", "/v1/memories/" + mid)]:
            response = await client.request(
                method, url, json={"status": "retired"}, headers={"Authorization": "Bearer viewer"}
            )
            assert response.status_code == 403
        response = await client.get(
            "/v1/memories?scope=invalid", headers={"Authorization": "Bearer member"}
        )
        assert response.status_code == 422
