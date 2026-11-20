"""Migration, active uniqueness, rollback and tenant RLS on real Postgres."""

import asyncio
import socket
from importlib import import_module
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError, NoResultFound

from sca.config import get_settings
from sca.db.schema import champions, evolutions

pytestmark = pytest.mark.integration


@pytest.fixture(scope='module', autouse=True)
def postgres_available():
    url = make_url(get_settings().database_url)
    try:
        with socket.create_connection((url.host or 'localhost', url.port or 5432), timeout=2):
            pass
    except OSError as exc:
        pytest.skip(f'Postgres unavailable: {type(exc).__name__}')


@pytest.fixture
async def evolution_store(store):
    # The shared fixture's migration list is outside this task's permitted files.
    def migrate(conn):
        if not inspect(conn).has_table('evolutions'):
            with Operations.context(MigrationContext.configure(conn)):
                import_module('sca.db.migrations.versions.0015_evolution').upgrade()

    async with store.sessionmaker.kw['bind'].begin() as conn:
        await conn.run_sync(migrate)
    return store


async def test_champion_activation_uniqueness_and_rollback(evolution_store):
    store = evolution_store.for_tenant('eval')
    ids = [uuid4(), uuid4()]
    values = {'agent_version': 'v', 'model': 'm', 'overlay': {}, 'overlay_sha256': 'hash',
              'metrics': {}}
    await asyncio.gather(*(store.activate_champion(str(i), values) for i in ids))
    rows = await store.list_champions('v', 'm')
    assert len(rows) == 2
    assert sum(r['active'] for r in rows) == 1
    await store.activate_champion(str(ids[0]))
    assert next(r for r in await store.list_champions() if r['active'])['id'] == ids[0]
    with pytest.raises(IntegrityError):
        async with store._transaction() as session:
            await session.execute(insert(champions).values(
                id=uuid4(), tenant_id='eval', **values, active=True))


async def test_evolution_journal_and_rls(evolution_store, api_store):
    store = evolution_store.for_tenant('eval')
    evolution_id, candidate_id = uuid4(), uuid4()
    await store.create_evolution({'id': evolution_id, 'status': 'running', 'config': {}, 'created_by': 'test'})
    candidate = {'id': candidate_id, 'evolution_id': evolution_id, 'generation': 1,
                 'overlay': {}, 'overlay_sha256': 'hash', 'hypothesis': 'test', 'diagnosis': {},
                 'static_check': {'valid': True}, 'decision': 'pending'}
    await store.save_evolution_candidate(candidate)
    await store.save_evolution_candidate({**candidate, 'decision': 'rejected', 'reason': 'test'})
    detail = await store.get_evolution(str(evolution_id))
    assert len(detail['candidates']) == len(detail['journal']) == 1
    assert detail['journal'][0]['decision'] == 'rejected'
    assert len(await api_store.for_tenant('eval').list_evolutions()) == 1
    assert await api_store.for_tenant('other').list_evolutions() == []
    with pytest.raises(NoResultFound):
        await api_store.for_tenant('other').get_evolution(str(evolution_id))
    with pytest.raises(DBAPIError):
        async with api_store.for_tenant('other')._transaction() as session:
            await session.execute(insert(evolutions).values(
                id=uuid4(), tenant_id='eval', status='running', config={}, created_by='test'))
    await store.activate_champion(str(uuid4()), {'agent_version': 'v', 'model': 'm',
        'overlay': {}, 'overlay_sha256': 'hash', 'metrics': {}, 'candidate_id': candidate_id})
    async with api_store.for_tenant('other')._transaction() as session:
        assert not (await session.execute(select(champions))).all()
        assert not (await session.execute(text('SELECT * FROM evolution_candidates'))).all()
    assert await evolution_store.for_tenant('other').list_champions() == []
