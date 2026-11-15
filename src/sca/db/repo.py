from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sca.core.errors import ConflictError, NotFoundError, ScaError
from sca.core.interfaces import ObjectStore
from sca.core.models import TERMINAL_STATES, Artifact, Event, Task, TaskState, can_transition
from sca.db.schema import artifacts, events, sessions, tasks, tenants


def task_uuid(value: str | UUID) -> UUID:
    try:
        return UUID(str(value))
    except ValueError:
        raise NotFoundError("Task not found") from None


class QuotaExceededError(ScaError):
    pass


class Store:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        default_max_active_tasks: int = 20,
        tenant_id: str | None = None,
        objects: ObjectStore | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.default_max_active_tasks = default_max_active_tasks
        self.tenant_id = tenant_id
        self.objects = objects

    def for_tenant(self, tenant_id: str) -> "Store":
        return Store(self.sessionmaker, self.default_max_active_tasks, tenant_id, self.objects)

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker.begin() as session:
            if self.tenant_id is not None:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": self.tenant_id}
                )
            yield session

    async def set_tenant_quota(self, tenant_id: str, max_active_tasks: int) -> None:
        if self.tenant_id is not None and self.tenant_id != tenant_id:
            raise NotFoundError("Tenant not found")
        if max_active_tasks < 0:
            raise ValueError("Quota must be nonnegative")
        async with self._transaction() as session:
            stmt = pg_insert(tenants).values(id=tenant_id, max_active_tasks=max_active_tasks)
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[tenants.c.id], set_={"max_active_tasks": max_active_tasks}
                )
            )

    async def _task(
        self, session: AsyncSession, task_id: str | UUID, lock: bool = False
    ) -> RowMapping:
        stmt = select(tasks).where(tasks.c.id == task_uuid(task_id))
        if self.tenant_id is not None:
            stmt = stmt.where(tasks.c.tenant_id == self.tenant_id)
        if lock:
            stmt = stmt.with_for_update()
        row = (await session.execute(stmt)).mappings().first()
        if row is None:
            raise NotFoundError("Task not found")
        return row

    async def _event(
        self,
        session: AsyncSession,
        task_id: UUID,
        type: str,
        payload: dict,
        dedup_key: str | None = None,
    ) -> Event | None:
        task = await self._task(session, task_id)
        if task["events_archived_at"] is not None:
            raise ConflictError("Events are archived")
        if dedup_key is not None:
            existing = await session.scalar(
                select(events.c.id).where(
                    events.c.task_id == task_id, events.c.dedup_key == dedup_key
                )
            )
            if existing is not None:
                return None
        seq = await session.scalar(
            select(func.coalesce(func.max(events.c.seq), 0) + 1).where(events.c.task_id == task_id)
        )
        row = (
            (
                await session.execute(
                    insert(events)
                    .values(
                        tenant_id=(await self._task(session, task_id))["tenant_id"],
                        task_id=task_id,
                        seq=seq,
                        type=type,
                        payload=payload,
                        dedup_key=dedup_key,
                    )
                    .returning(events)
                )
            )
            .mappings()
            .one()
        )
        await session.execute(text("SELECT pg_notify('sca_events', :id)"), {"id": str(task_id)})
        return Event.model_validate(dict(row))

    async def create_task(
        self, *, tenant_id: str, kind: str, spec: dict, idempotency_key: str | None
    ) -> tuple[Task, bool]:
        async with self._transaction() as session:
            if self.tenant_id is not None and tenant_id != self.tenant_id:
                raise NotFoundError("Tenant not found")
            await session.execute(
                pg_insert(tenants)
                .values(id=tenant_id, max_active_tasks=self.default_max_active_tasks)
                .on_conflict_do_nothing(index_elements=[tenants.c.id])
            )
            quota = await session.scalar(
                select(tenants.c.max_active_tasks)
                .where(tenants.c.id == tenant_id)
                .with_for_update()
            )
            if idempotency_key is not None:
                existing = (
                    (
                        await session.execute(
                            select(tasks).where(
                                tasks.c.tenant_id == tenant_id,
                                tasks.c.idempotency_key == idempotency_key,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing is not None:
                    return Task.model_validate(dict(existing)), False
            active = await session.scalar(
                select(func.count())
                .select_from(tasks)
                .where(tasks.c.tenant_id == tenant_id, tasks.c.state.not_in(TERMINAL_STATES))
            )
            if active >= quota:
                raise QuotaExceededError("Active task quota exceeded")
            stmt = pg_insert(tasks).values(
                id=uuid4(),
                tenant_id=tenant_id,
                kind=kind,
                spec=spec,
                state=TaskState.QUEUED,
                idempotency_key=idempotency_key,
            )
            stmt = stmt.on_conflict_do_nothing(
                index_elements=[tasks.c.tenant_id, tasks.c.idempotency_key]
            ).returning(tasks)
            row = (await session.execute(stmt)).mappings().first()
            if row is None:
                row = (
                    (
                        await session.execute(
                            select(tasks).where(
                                tasks.c.tenant_id == tenant_id,
                                tasks.c.idempotency_key == idempotency_key,
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                return Task.model_validate(dict(row)), False
            await self._event(session, row["id"], "task.created", {"spec": spec})
            return Task.model_validate(dict(row)), True

    async def get_task(self, task_id: str | UUID, tenant_id: str | None = None) -> Task:
        async with self._transaction() as session:
            row = await self._task(session, task_id)
            if tenant_id is not None and row["tenant_id"] != tenant_id:
                raise NotFoundError("Task not found")
            return Task.model_validate(dict(row))

    async def list_tasks(
        self, tenant_id: str, state: TaskState | None = None, limit: int = 50
    ) -> list[Task]:
        async with self._transaction() as session:
            stmt = select(tasks).where(tasks.c.tenant_id == tenant_id)
            if self.tenant_id is not None:
                stmt = stmt.where(tasks.c.tenant_id == self.tenant_id)
            if state is not None:
                stmt = stmt.where(tasks.c.state == state)
            rows = (
                await session.execute(stmt.order_by(tasks.c.created_at.desc()).limit(limit))
            ).mappings()
            return [Task.model_validate(dict(row)) for row in rows]

    async def set_state(
        self, task_id: str | UUID, new_state: TaskState, reason: str | None = None
    ) -> Task:
        async with self._transaction() as session:
            row = await self._task(session, task_id, True)
            old = TaskState(row["state"])
            if old == new_state:
                return Task.model_validate(dict(row))
            if not can_transition(old, new_state):
                raise ConflictError(f"Cannot transition from {old} to {new_state}")
            updated = (
                (
                    await session.execute(
                        update(tasks)
                        .where(tasks.c.id == row["id"])
                        .values(state=new_state, state_reason=reason, updated_at=func.now())
                        .returning(tasks)
                    )
                )
                .mappings()
                .one()
            )
            await self._event(
                session,
                row["id"],
                "task.state_changed",
                {"from": old, "to": new_state, "reason": reason},
            )
            return Task.model_validate(dict(updated))

    async def append_event(
        self, task_id: str | UUID, type: str, payload: dict, dedup_key: str | None = None
    ) -> Event | None:
        async with self._transaction() as session:
            row = await self._task(session, task_id, True)
            return await self._event(session, row["id"], type, payload, dedup_key)

    async def list_events(
        self, task_id: str | UUID, after: int = 0, limit: int = 500
    ) -> list[Event]:
        async with self._transaction() as session:
            task = await self._task(session, task_id, True)
            if task["events_archived_at"] is not None:
                from sca.archive import read_events

                if self.objects is None:
                    raise RuntimeError("Archived events require an object store")
                archived = await read_events(self.objects, task_id)
                return [event for event in archived if event.seq > after][:limit]
            rows = (
                await session.execute(
                    select(events)
                    .where(events.c.task_id == task_uuid(task_id), events.c.seq > after)
                    .order_by(events.c.seq)
                    .limit(limit)
                )
            ).mappings()
            return [Event.model_validate(dict(row)) for row in rows]

    async def _merge(
        self, task_id: str | UUID, column: str, patch: dict, sum_values: bool = False
    ) -> None:
        async with self._transaction() as session:
            row = await self._task(session, task_id, True)
            value = dict(row[column])
            for key, item in patch.items():
                value[key] = value.get(key, 0) + item if sum_values else item
            await session.execute(
                update(tasks)
                .where(tasks.c.id == row["id"])
                .values(**{column: value}, updated_at=func.now())
            )

    async def update_result(self, task_id: str | UUID, patch: dict) -> None:
        await self._merge(task_id, "result", patch)

    async def add_usage(self, task_id: str | UUID, usage: dict[str, int]) -> None:
        await self._merge(task_id, "usage", usage, True)

    async def set_session(self, task_id: str | UUID, backend: str, backend_session_id: str) -> None:
        async with self._transaction() as session:
            row = await self._task(session, task_id, True)
            sid = await session.scalar(select(sessions.c.id).where(sessions.c.task_id == row["id"]))
            values = {"backend": backend, "backend_session_id": backend_session_id}
            if sid is None:
                await session.execute(
                    insert(sessions).values(
                        id=uuid4(), tenant_id=row["tenant_id"], task_id=row["id"], **values
                    )
                )
            else:
                await session.execute(update(sessions).where(sessions.c.id == sid).values(**values))

    async def get_session_id(self, task_id: str | UUID) -> str | None:
        async with self._transaction() as session:
            await self._task(session, task_id)
            return await session.scalar(
                select(sessions.c.backend_session_id).where(
                    sessions.c.task_id == task_uuid(task_id)
                )
            )

    async def add_artifact(
        self, task_id: str | UUID, name: str, content_type: str, uri: str, size_bytes: int
    ) -> Artifact:
        async with self._transaction() as session:
            task = await self._task(session, task_id)
            values: dict[str, Any] = {
                "content_type": content_type,
                "uri": uri,
                "size_bytes": size_bytes,
            }
            stmt = pg_insert(artifacts).values(
                id=uuid4(),
                tenant_id=task["tenant_id"],
                task_id=task_uuid(task_id),
                name=name,
                **values,
            )
            row = (
                (
                    await session.execute(
                        stmt.on_conflict_do_update(
                            index_elements=[artifacts.c.task_id, artifacts.c.name],
                            set_={**values, "created_at": func.now()},
                        ).returning(artifacts)
                    )
                )
                .mappings()
                .one()
            )
            return Artifact.model_validate(dict(row))

    async def list_artifacts(self, task_id: str | UUID) -> list[Artifact]:
        async with self._transaction() as session:
            await self._task(session, task_id)
            rows = (
                await session.execute(
                    select(artifacts)
                    .where(artifacts.c.task_id == task_uuid(task_id))
                    .order_by(artifacts.c.name)
                )
            ).mappings()
            return [Artifact.model_validate(dict(row)) for row in rows]

    async def get_artifact(self, task_id: str | UUID, name: str) -> Artifact:
        async with self._transaction() as session:
            await self._task(session, task_id)
            row = (
                (
                    await session.execute(
                        select(artifacts).where(
                            artifacts.c.task_id == task_uuid(task_id), artifacts.c.name == name
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFoundError("Artifact not found")
            return Artifact.model_validate(dict(row))

    async def set_tenant_budget(self, tenant_id: str, daily_token_budget: int | None) -> None:
        if self.tenant_id is not None and self.tenant_id != tenant_id:
            raise NotFoundError("Tenant not found")
        if daily_token_budget is not None and daily_token_budget < 0:
            raise ValueError("Budget must be nonnegative")
        async with self._transaction() as session:
            stmt = pg_insert(tenants).values(
                id=tenant_id,
                max_active_tasks=self.default_max_active_tasks,
                daily_token_budget=daily_token_budget,
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[tenants.c.id], set_={"daily_token_budget": daily_token_budget}
                )
            )

    async def check_budget(self, task_id: str | UUID) -> str | None:
        def tokens(usage: dict) -> int:
            return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))

        async with self._transaction() as session:
            task = await self._task(session, task_id)
            maximum = task["spec"].get("max_tokens")
            if maximum is not None and tokens(task["usage"]) >= maximum:
                return f"Task token budget exhausted ({maximum} tokens)"
            budget = await session.scalar(
                select(tenants.c.daily_token_budget).where(tenants.c.id == task["tenant_id"])
            )
            if budget is not None:
                midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
                usages = await session.scalars(
                    select(tasks.c.usage).where(
                        tasks.c.tenant_id == task["tenant_id"], tasks.c.created_at >= midnight
                    )
                )
                if sum(tokens(usage) for usage in usages) >= budget:
                    return f"Tenant daily token budget exhausted ({budget} tokens)"
            return None
