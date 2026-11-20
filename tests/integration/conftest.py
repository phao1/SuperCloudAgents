from importlib import import_module
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from sca.config import get_settings
from sca.db.engine import make_sessionmaker
from sca.db.repo import Store


@pytest.fixture
async def store():
    database = "test_" + uuid4().hex
    url = make_url(get_settings().database_url)
    admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{database}"'))
    engine = create_async_engine(url.set(database=database))
    try:

        def migrate(conn):
            with Operations.context(MigrationContext.configure(conn)):
                for revision in (
                    "0001_initial",
                    "0002_tenancy",
                    "0003_archive_budget",
                    "0004_api_tokens",
                    "0005_cost_budget",
                    "0006_operations",
                    "0007_files",
                    "0008_memories",
                    "0009_evaluation",
                    "0010_memory_vectors",
                    "0011_eval_tenancy",
                    "0012_eval_trajectory",
                    "0013_eval_run_insert",
                    "0014_admission",
                    "0015_evolution",
                    "0020_tenancy_billing",
                ):
                    import_module(f"sca.db.migrations.versions.{revision}").upgrade()

        async with engine.begin() as conn:
            await conn.run_sync(migrate)
            await conn.execute(text("ALTER ROLE sca_api LOGIN PASSWORD 'sca-api-dev'"))
        yield Store(make_sessionmaker(engine))
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        await admin.dispose()


@pytest.fixture
async def api_store(store):
    url = store.sessionmaker.kw["bind"].url.set(username="sca_api", password="sca-api-dev")
    engine = create_async_engine(url)
    try:
        yield Store(make_sessionmaker(engine))
    finally:
        await engine.dispose()
