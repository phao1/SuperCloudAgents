import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_api import api  # noqa: F401

from sca.api.app import create_app
from sca.api.auth import OidcAuthenticator
from sca.core.errors import NotFoundError
from sca.core.models import TaskState
from sca.db.repo import QuotaExceededError

pytestmark = pytest.mark.integration


async def create(store, tenant, key=None):
    task, _ = await store.create_task(tenant_id=tenant, kind="coding", spec={}, idempotency_key=key)
    await store.set_session(task.id, "fake", "session")
    await store.add_artifact(task.id, "patch", "text/plain", "file:///tmp/patch", 1)
    return task


async def test_raw_rls(store, api_store):
    a = await create(store, "a")
    await create(store, "b")
    for tenant in (None, "a"):
        async with api_store.sessionmaker.begin() as session:
            if tenant:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant}
                )
            for table in ("tasks", "events", "sessions", "artifacts"):
                rows = (await session.execute(text(f"SELECT * FROM {table}"))).mappings().all()
                assert {r["tenant_id"] for r in rows} == ({"a"} if tenant else set())
    for table in ("tasks", "events", "sessions", "artifacts"):
        async with api_store.sessionmaker.begin() as session:
            await session.execute(text("SELECT set_config('app.tenant_id', 'a', true)"))
            result = await session.execute(
                text(f"UPDATE {table} SET tenant_id='a' WHERE tenant_id='b'")
            )
            assert result.rowcount == 0
        with pytest.raises(DBAPIError):
            async with api_store.sessionmaker.begin() as session:
                await session.execute(text("SELECT set_config('app.tenant_id', 'a', true)"))
                await session.execute(text(f"UPDATE {table} SET tenant_id='b'"))
    statements = [
        (
            "INSERT INTO tasks (id,tenant_id,kind,spec,state) VALUES (:id,'b','coding','{}','QUEUED')",
            {"id": uuid4()},
        ),
        (
            "INSERT INTO events (tenant_id,task_id,seq,type) VALUES ('b',:id,99,'test')",
            {"id": a.id},
        ),
        (
            "INSERT INTO sessions (id,tenant_id,task_id,backend) VALUES (:sid,'b',:id,'fake')",
            {"sid": uuid4(), "id": a.id},
        ),
        (
            (
                "INSERT INTO artifacts (id,tenant_id,task_id,name,content_type,uri,size_bytes) "
                "VALUES (:sid,'b',:id,'other','text/plain','file:///tmp/x',1)"
            ),
            {"sid": uuid4(), "id": a.id},
        ),
    ]
    for statement, params in statements:
        with pytest.raises(DBAPIError):
            async with api_store.sessionmaker.begin() as session:
                await session.execute(text("SELECT set_config('app.tenant_id', 'a', true)"))
                await session.execute(text(statement), params)
    with pytest.raises(NotFoundError):
        await api_store.for_tenant("b").get_task(a.id)
    # Explicit scoping also protects callers using the owner connection.
    with pytest.raises(NotFoundError):
        await store.for_tenant("b").get_artifact(a.id, "patch")


async def test_quota_concurrency(store, api_store):
    await store.set_tenant_quota("a", 2)
    scoped = api_store.for_tenant("a")
    results = await asyncio.gather(
        *(create(scoped, "a") for _ in range(10)), return_exceptions=True
    )
    assert sum(isinstance(r, QuotaExceededError) for r in results) == 8
    assert len(await scoped.list_tasks("a")) == 2
    first = next(r for r in results if not isinstance(r, Exception))
    await store.set_state(first.id, TaskState.CANCELLED)
    await create(scoped, "a", "replay")
    replay, created = await scoped.create_task(
        tenant_id="a", kind="coding", spec={}, idempotency_key="replay"
    )
    assert not created and replay.id != first.id
    assert len(await scoped.list_tasks("a")) == 3


async def test_api_quota(api, store):  # noqa: F811
    from test_api import submit

    client, _, _ = api
    await store.set_tenant_quota("one", 1)
    first = await submit(client, "key")
    assert first.status_code == 201
    assert (await submit(client, "key")).json()["id"] == first.json()["id"]
    response = await submit(client)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "quota_exceeded"
    await store.set_state(first.json()["id"], TaskState.CANCELLED)
    assert (await submit(client)).status_code == 201


async def test_api_oidc(api_store, store, monkeypatch):
    import time

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from test_api import FakeOrchestrator

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = OidcAuthenticator("http://issuer", "sca", "http://issuer/keys")
    jwk = jwt.PyJWK.from_json(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    monkeypatch.setattr(auth.jwks, "fetch_data", lambda: {"keys": [{**jwk._jwk_data, "kid": "k"}]})
    token = jwt.encode(
        {
            "iss": "http://issuer",
            "aud": "sca",
            "sub": "user",
            "tenant": "a",
            "exp": time.time() + 60,
        },
        key,
        algorithm="RS256",
        headers={"kid": "k"},
    )
    app = create_app(store=api_store, orchestrator=FakeOrchestrator(store), authenticator=auth)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer " + token},
    ) as client:
        response = await client.post(
            "/v1/tasks",
            json={"kind": "coding", "spec": {"repo_url": "/tmp/repo", "instruction": "fix"}},
        )
        assert response.status_code == 201
        assert response.json()["tenant_id"] == "a"


async def test_concurrent_api_submits(api, store):  # noqa: F811
    from test_api import submit

    client, _, _ = api
    await store.set_tenant_quota("one", 2)
    responses = await asyncio.gather(*(submit(client) for _ in range(10)))
    assert sorted(r.status_code for r in responses) == [201] * 2 + [429] * 8
    assert len((await client.get("/v1/tasks")).json()) == 2
