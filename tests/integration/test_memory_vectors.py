import pytest
from sqlalchemy import text

from sca.config import Settings
from sca.memory import embedding

pytestmark = pytest.mark.integration


class FakeGateway:
    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        return [[1.0, 0.0] if 'database' in value else [0.0, 1.0] for value in texts]


async def make_task(store):
    return (await store.create_task(tenant_id='one', kind='analysis',
                                   spec={'question': 'database'}, idempotency_key=None))[0]


async def test_recent_fallback(store):
    task = await make_task(store)
    tenant = store.for_tenant('one')
    await tenant.create_memory({'text': 'old database', 'scope': 'tenant'}, 'alice')
    await tenant.create_memory({'text': 'new unrelated', 'scope': 'tenant'}, 'alice')
    content = await tenant.inject_memories(str(task.id), 1, 3000,
                                          settings=Settings(memory_embedding_model=None))
    assert 'new unrelated' in content and 'old database' not in content
    assert (await store.list_events(task.id))[-1].payload['method'] == 'recent'


async def test_vector_relevance_categories_and_reindex(store, api_store, monkeypatch):
    async with store._transaction() as session:
        if not await store._has_memory_vectors(session):
            pytest.skip('pgvector extension unavailable in test database')
    settings = Settings(memory_embedding_model='fake', memory_max_items=3,
                        memory_min_per_category=1)
    monkeypatch.setattr(embedding, 'get_settings', lambda: settings)
    gateway = FakeGateway()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_gateway(settings, gateway=None):
        yield FakeGateway()

    monkeypatch.setattr(embedding, 'embedding_gateway', fake_gateway)
    task = await make_task(store)
    tenant = api_store.for_tenant('one')
    for value, category in [('database index', 'convention'), ('run tests', 'command'),
                            ('avoid errors', 'pitfall'), ('unrelated new', 'convention')]:
        await tenant.create_memory({'text': value, 'scope': 'tenant', 'category': category}, 'alice',
                                   vector=await embedding.embed(value, settings, gateway))
    query = await tenant.memory_query(str(task.id), 3, 3000)
    assert query == 'database'
    content = await tenant.inject_memories(str(task.id), 3, 3000,
                                          settings=settings, vector=await embedding.embed(query, settings, gateway))
    assert all(v in content for v in ('database index', 'run tests', 'avoid errors'))
    assert 'unrelated new' not in content
    assert (await store.list_events(task.id))[-1].payload['method'] == 'vector'
    async with tenant._transaction() as session:
        await session.execute(text("UPDATE memories SET embedding = NULL WHERE tenant_id = 'one'"))
    async def reindex(target):
        count = 0
        for row in await target.list_memories(status='active', missing_vectors=True):
            count += await target.reindex_memories(row, await embedding.embed(row['text']))
        return count

    assert await reindex(tenant) == 4
    assert await reindex(tenant) == 0
    assert await reindex(api_store.for_tenant('two')) == 0


async def test_missing_column_fallback(store, monkeypatch):
    async def missing(session):
        return False

    tenant = store.for_tenant('one')
    monkeypatch.setattr(tenant, '_has_memory_vectors', missing)
    task = await make_task(store)
    await tenant.create_memory({'scope': 'tenant', 'text': 'first'}, 'alice')
    await tenant.create_memory({'scope': 'tenant', 'text': 'second'}, 'alice')
    content = await tenant.inject_memories(str(task.id), 1, 3000,
                                          settings=Settings(memory_embedding_model='fake'),
                                          vector=[1.0, 0.0])
    assert 'second' in content and 'first' not in content
    assert (await store.list_events(task.id))[-1].payload['method'] == 'recent'


async def test_api_embeddings_release_connections_and_store_current_text(
    store, api_store, tmp_path, monkeypatch,
):
    import httpx

    from sca.api.app import create_app
    from sca.api.auth import StaticTokenAuthenticator
    from sca.storage.local import LocalObjectStore

    calls = []

    async def fake_embed(value):
        assert store.sessionmaker.kw['bind'].pool.checkedout() == 0
        assert api_store.sessionmaker.kw['bind'].pool.checkedout() == 0
        calls.append(value)
        return [1.0, 0.0]

    monkeypatch.setattr('sca.api.memories.embed', fake_embed)
    app = create_app(store=api_store, orchestrator=None,
                     authenticator=StaticTokenAuthenticator({'test': 'one'}),
                     object_store=LocalObjectStore(tmp_path))
    tenant = api_store.for_tenant('one')
    task = await make_task(store)
    ids = await tenant.propose_memories(str(task.id), 'agent_proposal', [{'text': 'proposal'}])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                headers={'Authorization': 'Bearer test'}) as client:
        created = await client.post('/v1/memories', json={'text': 'original', 'scope': 'tenant'})
        assert created.status_code == 201
        memory_id = created.json()['id']
        assert (await client.patch('/v1/memories/' + memory_id,
                                   json={'text': 'edited'})).status_code == 200
        assert (await client.patch('/v1/memories/' + ids[0],
                                   json={'status': 'active'})).status_code == 200
        assert (await client.post('/v1/memories/reindex')).status_code == 200
    assert calls[:3] == ['original', 'edited', 'proposal']
    row = next(r for r in await tenant.list_memories() if str(r['id']) == memory_id)
    await tenant.change_memory(row['id'], 'alice', {'text': 'newer'})
    assert await tenant.reindex_memories(row, [1.0, 0.0]) == 0
    await tenant.change_memory(row['id'], 'alice', {'text': 'latest'},
                               vector=[1.0, 0.0], vector_text='stale')
    async with tenant._transaction() as session:
        assert await session.scalar(text('SELECT embedding IS NULL FROM memories WHERE id = :id'),
                                    {'id': row['id']})


async def test_kernel_embeds_between_transactions_and_only_when_needed(store, tmp_path):
    from types import SimpleNamespace

    from sca.kernel.activities import KernelActivities
    from sca.storage.local import LocalObjectStore

    class Gateway(FakeGateway):
        calls = 0

        async def embed(self, texts, model):
            assert store.sessionmaker.kw['bind'].pool.checkedout() == 0
            self.calls += 1
            return await super().embed(texts, model)

    task = await make_task(store)
    tenant = store.for_tenant('one')
    await tenant.create_memory({'text': 'database', 'scope': 'tenant'}, 'alice', vector=[1.0, 0.0])
    await tenant.create_memory({'text': 'unrelated', 'scope': 'tenant'}, 'alice', vector=[0.0, 1.0])
    assert await tenant.memory_query(str(task.id), 30, 3000) is None
    gateway = Gateway()
    acts = KernelActivities(
        settings=Settings(data_dir=tmp_path, memory_max_items=1, memory_embedding_model='fake'),
        store=store, sandbox=object(), agent=SimpleNamespace(name='fake'),
        objects=LocalObjectStore(tmp_path), gateway=gateway,
    )
    acts._workspace(str(task.id)).workspace.mkdir(parents=True)
    await acts.memory_initial_prompt(str(task.id))
    await acts.memory_initial_prompt(str(task.id))
    assert gateway.calls == 1
    events = [e for e in await store.list_events(task.id) if e.type == 'memory.injected']
    assert len(events) == 1 and events[0].payload['method'] == 'vector'
    content = (acts._workspace(str(task.id)).workspace / '.sca/context/MEMORY.md').read_text()
    assert 'database' in content and 'unrelated' not in content


async def test_review_acceptance_embeds_outside_the_transaction(store, api_store, tmp_path,
                                                                monkeypatch):
    import httpx

    from sca.api.app import create_app
    from sca.api.auth import StaticTokenAuthenticator
    from sca.core.models import TaskState
    from sca.storage.local import LocalObjectStore

    class Orchestrator:
        async def submit_review(self, *args):
            pass

    calls = []

    async def fake_embed(value):
        assert api_store.sessionmaker.kw['bind'].pool.checkedout() == 0
        calls.append(value)
        return [1.0, 0.0]

    monkeypatch.setattr(embedding, 'embed', fake_embed)
    task = await make_task(store)
    tenant = api_store.for_tenant('one')
    ids = await tenant.propose_memories(str(task.id), 'agent_proposal', [{'text': 'use pnpm'}])
    for state in (TaskState.PROVISIONING, TaskState.RUNNING, TaskState.VERIFYING,
                  TaskState.AWAITING_REVIEW):
        await store.set_state(task.id, state)
    app = create_app(store=api_store, orchestrator=Orchestrator(),
                     authenticator=StaticTokenAuthenticator({'test': 'one'}),
                     object_store=LocalObjectStore(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                headers={'Authorization': 'Bearer test'}) as client:
        response = await client.post(f'/v1/tasks/{task.id}/review',
                                     json={'decision': 'approve', 'accept_memories': ids})
        assert response.status_code == 200, response.text
    assert calls == ['use pnpm']
    async with tenant._transaction() as session:
        assert await session.scalar(
            text('SELECT embedding IS NOT NULL FROM memories WHERE id = :id'), {'id': ids[0]})
