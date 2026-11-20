from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sca.config import Settings
from sca.core.errors import ConflictError, NotFoundError, ScaError
from sca.core.interfaces import ObjectStore
from sca.core.models import TERMINAL_STATES, Artifact, Event, Task, TaskState, can_transition
from sca.db.schema import api_tokens, artifacts, events, sessions, tasks, tenants


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

    async def create_file(self, file_id: UUID, name: str, size_bytes: int, sha256: str) -> dict:
        from sca.db.schema import files

        if self.tenant_id is None:
            raise ValueError("file creation requires a tenant")
        async with self._transaction() as session:
            row = (await session.execute(insert(files).values(
                id=file_id, tenant_id=self.tenant_id, name=name,
                size_bytes=size_bytes, sha256=sha256,
            ).returning(files))).mappings().one()
            return dict(row)

    async def get_file(self, file_id: UUID) -> dict:
        from sca.db.schema import files

        if self.tenant_id is None:
            raise ValueError("file lookup requires a tenant")
        async with self._transaction() as session:
            row = (await session.execute(select(files).where(
                files.c.id == file_id, files.c.tenant_id == self.tenant_id,
            ))).mappings().first()
            if row is None:
                raise NotFoundError("File not found")
            return dict(row)

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
        self,
        *,
        tenant_id: str,
        kind: str,
        spec: dict,
        idempotency_key: str | None,
        actor: str | None = None,
    ) -> tuple[Task, bool]:
        async with self._transaction() as session:
            if self.tenant_id is not None and tenant_id != self.tenant_id:
                raise NotFoundError("Tenant not found")
            await session.execute(
                pg_insert(tenants)
                .values(id=tenant_id, max_active_tasks=self.default_max_active_tasks)
                .on_conflict_do_nothing(index_elements=[tenants.c.id])
            )
            await session.scalar(
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
                .where(tasks.c.tenant_id == tenant_id, tasks.c.state == TaskState.QUEUED)
            )
            from sca.config import get_settings

            if active >= get_settings().tenant_max_queued:
                raise QuotaExceededError("Queued task backlog exceeded")
            if spec.get("priority", "interactive") not in ("interactive", "eval", "batch"):
                raise ValueError("Invalid admission priority")
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
            await self._event(session, row["id"], "task.created", {"spec": spec, "actor": actor})
            return Task.model_validate(dict(row)), True

    async def acquire_slot(self, task_id: str, maximum: int, saturated: bool = False) -> dict:
        """Worker-only global scheduling; never call using the RLS API role."""
        from collections import Counter

        from sca.admission.order import grant_order

        if self.tenant_id is not None:
            raise ValueError("Admission requires the worker store")
        async with self._transaction() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(73421045)"))
            task = await self._task(session, task_id, True)
            if task["state"] in TERMINAL_STATES:
                return {"granted": False, "terminal": True}
            if task["slot_held"]:
                return {"granted": True}
            if task["slot_requested_at"] is None:
                task = (await session.execute(update(tasks).where(tasks.c.id == task["id"])
                    .values(slot_requested_at=func.now(), slot_generation=tasks.c.slot_generation + 1)
                    .returning(tasks))).mappings().one()
            held = Counter((await session.execute(select(tasks.c.tenant_id).where(
                tasks.c.slot_held))).scalars().all())
            quotas = (await session.execute(select(tenants))).mappings().all()
            weights = {r["id"]: r["weight"] for r in quotas}
            limits = {r["id"]: r["max_active_tasks"] for r in quotas}
            candidates = (await session.execute(select(tasks).where(
                tasks.c.slot_requested_at.is_not(None), ~tasks.c.slot_held,
                tasks.c.state.not_in(TERMINAL_STATES)))).mappings().all()
            ordered = sorted(candidates, key=lambda r: grant_order(dict(r), held, weights))
            eligible = [r for r in ordered if held[r["tenant_id"]] < limits[r["tenant_id"]]]
            position = next(i + 1 for i, r in enumerate(ordered) if r["id"] == task["id"])
            reason = ("gateway_pressure" if saturated else "global_limit"
                      if sum(held.values()) >= maximum else "tenant_limit"
                      if held[task["tenant_id"]] >= limits[task["tenant_id"]] else "fair_order")
            key = f"admission.{task['slot_generation']}"
            if not saturated and sum(held.values()) < maximum and eligible and eligible[0]["id"] == task["id"]:
                waited = (datetime.now(UTC) - task["slot_requested_at"]).total_seconds()
                await session.execute(update(tasks).where(tasks.c.id == task["id"]).values(
                    slot_held=True, state_reason=None,
                    result={**task["result"], "admission": {"granted": True}}))
                await self._event(session, task["id"], "admission.granted",
                                  {"wait_seconds": waited}, key + ".granted")
                return {"granted": True, "wait_seconds": waited}
            await session.execute(update(tasks).where(tasks.c.id == task["id"]).values(
                result={**task["result"], "admission": {"position": position, "reason": reason}}))
            await self._event(session, task["id"], "admission.waiting",
                              {"position": position, "reason": reason}, key + ".waiting")
            return {"granted": False, "position": position, "reason": reason}

    async def release_slot(self, task_id: str) -> None:
        async with self._transaction() as session:
            row = await self._task(session, task_id, True)
            await session.execute(update(tasks).where(tasks.c.id == row["id"]).values(
                slot_held=False, slot_requested_at=None,
                result={**row["result"], "admission": {}}))

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
                        .values(state=new_state, state_reason=reason, updated_at=func.now(),
                                **({"slot_held": False, "slot_requested_at": None,
                                    "result": {**row["result"], "admission": {}}}
                                   if new_state in TERMINAL_STATES | {TaskState.AWAITING_REVIEW, TaskState.PAUSED}
                                   else {}))
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

    async def set_tenant_budget(
        self,
        tenant_id: str,
        daily_token_budget: int | None = None,
        daily_cost_budget_usd: float | None = None,
    ) -> None:
        if self.tenant_id is not None and self.tenant_id != tenant_id:
            raise NotFoundError("Tenant not found")
        if daily_token_budget is not None and daily_token_budget < 0:
            raise ValueError("Budget must be nonnegative")
        if daily_cost_budget_usd is not None and (
            not Decimal(str(daily_cost_budget_usd)).is_finite() or daily_cost_budget_usd < 0
        ):
            raise ValueError("Budget must be finite and nonnegative")
        cost_budget = (
            Decimal(str(daily_cost_budget_usd)) if daily_cost_budget_usd is not None else None
        )
        async with self._transaction() as session:
            stmt = pg_insert(tenants).values(
                id=tenant_id,
                max_active_tasks=self.default_max_active_tasks,
                daily_token_budget=daily_token_budget,
                daily_cost_budget_usd=cost_budget,
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[tenants.c.id],
                    set_={
                        "daily_token_budget": daily_token_budget,
                        "daily_cost_budget_usd": cost_budget,
                    },
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
            maximum_cost = task["spec"].get("max_cost_usd")
            if maximum_cost is not None and Decimal(task["usage"].get("cost_micro_usd", 0)) >= (
                Decimal(str(maximum_cost)) * 1_000_000
            ):
                return f"Task cost budget exhausted ({maximum_cost} USD)"
            tenant = (
                (await session.execute(select(tenants).where(tenants.c.id == task["tenant_id"])))
                .mappings()
                .one()
            )
            budget = tenant["daily_token_budget"]
            cost_budget = tenant["daily_cost_budget_usd"]
            if budget is not None or cost_budget is not None:
                midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
                usages = list(
                    await session.scalars(
                        select(tasks.c.usage).where(
                            tasks.c.tenant_id == task["tenant_id"], tasks.c.created_at >= midnight
                        )
                    )
                )
                if budget is not None and sum(tokens(usage) for usage in usages) >= budget:
                    return f"Tenant daily token budget exhausted ({budget} tokens)"
                if (
                    cost_budget is not None
                    and sum(usage.get("cost_micro_usd", 0) for usage in usages)
                    >= cost_budget * 1_000_000
                ):
                    return f"Tenant daily cost budget exhausted ({cost_budget} USD)"
            return None

    async def lookup_api_token(self, token_hash: str) -> dict[str, Any] | None:
        async with self.sessionmaker.begin() as session:
            row = (
                (
                    await session.execute(
                        text("SELECT * FROM public.sca_token_lookup(:hash)"), {"hash": token_hash}
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def create_api_token(self, **values: Any) -> dict[str, Any]:
        if self.tenant_id is None:
            raise ValueError("Tenant scope required")
        async with self._transaction() as session:
            row = (
                (
                    await session.execute(
                        insert(api_tokens)
                        .values(id=uuid4(), tenant_id=self.tenant_id, **values)
                        .returning(api_tokens)
                    )
                )
                .mappings()
                .one()
            )
            return {k: v for k, v in row.items() if k != "token_hash"}

    async def list_api_tokens(self) -> list[dict[str, Any]]:
        async with self._transaction() as session:
            rows = (
                await session.execute(
                    select(*[c for c in api_tokens.c if c.name != "token_hash"])
                    .where(api_tokens.c.tenant_id == self.tenant_id)
                    .order_by(api_tokens.c.created_at.desc())
                )
            ).mappings()
            return [dict(row) for row in rows]

    async def revoke_api_token(self, token_id: UUID) -> None:
        async with self._transaction() as session:
            result = await session.execute(
                update(api_tokens)
                .where(api_tokens.c.id == token_id, api_tokens.c.tenant_id == self.tenant_id)
                .values(revoked_at=func.coalesce(api_tokens.c.revoked_at, func.now()))
            )
            if not result.rowcount:
                raise NotFoundError("Token not found")

    async def _has_memory_vectors(self, session: AsyncSession) -> bool:
        return bool(await session.scalar(text("""SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = 'memories'
            AND column_name = 'embedding')""")))

    async def _store_memory_vector(self, session: AsyncSession, row: dict,
                            vector: list[float] | None) -> bool:
        from sca.memory.embedding import literal

        if not await self._has_memory_vectors(session):
            return False
        if vector is None:
            return False
        import logging

        try:
            async with session.begin_nested():
                await session.execute(text("""UPDATE memories SET embedding = CAST(:v AS vector)
                    WHERE id = :id AND tenant_id = :tenant AND text = :content
                    AND status = 'active'"""),
                    {'v': literal(vector), 'id': row['id'], 'tenant': self.tenant_id,
                     'content': row['text']})
            return True
        except Exception:  # noqa: BLE001 - optional embeddings must never block approval
            logging.getLogger(__name__).warning("Memory embedding storage unavailable")
            return False

    async def reindex_memories(self, row: dict, vector: list[float] | None) -> int:
        async with self._transaction() as session:
            if not await self._has_memory_vectors(session):
                return 0
            missing = await session.scalar(text("""SELECT embedding IS NULL FROM memories
                WHERE id = :id AND tenant_id = :tenant AND text = :content
                AND status = 'active'"""),
                {'id': row['id'], 'tenant': self.tenant_id, 'content': row['text']})
            return int(bool(missing and await self._store_memory_vector(session, row, vector)))

    async def list_memories(self, scope: str | None = None, status: str | None = None,
                            *, missing_vectors: bool = False) -> list[dict]:
        from sca.db.schema import memories
        from sca.memory.models import normalise_scope

        if self.tenant_id is None:
            raise ValueError('memories require a tenant')
        stmt = select(memories).where(memories.c.tenant_id == self.tenant_id)
        if scope is not None:
            stmt = stmt.where(memories.c.scope == normalise_scope(scope))
        if status is not None:
            stmt = stmt.where(memories.c.status == status)
        stmt = stmt.order_by(memories.c.last_used_at.desc().nullslast(),
                             memories.c.created_at.desc(), memories.c.id)
        async with self._transaction() as session:
            if missing_vectors:
                if not await self._has_memory_vectors(session):
                    return []
                stmt = stmt.where(text('embedding IS NULL'))
            return [dict(row) for row in (await session.execute(stmt)).mappings()]

    async def _memory_audit(self, session: AsyncSession, memory_id: UUID,
                            actor: str, action: str) -> None:
        from sca.db.schema import memory_events

        await session.execute(insert(memory_events).values(
            id=uuid4(), tenant_id=self.tenant_id, memory_id=memory_id, actor=actor, action=action,
        ))

    async def _create_memory(self, session: AsyncSession, body: dict, actor: str,
                             source: str, source_task_id: UUID | None = None,
                             vector: list[float] | None = None) -> dict:
        from sca.db.schema import memories
        from sca.memory.models import MemoryInput

        value = MemoryInput.model_validate(body)
        if source_task_id is not None:
            await self._task(session, source_task_id)
        now = datetime.now(UTC)
        row = (await session.execute(insert(memories).values(
            id=uuid4(), tenant_id=self.tenant_id, **value.model_dump(), source=source,
            source_task_id=source_task_id, created_by=actor,
            status='active' if source == 'manual' else 'proposed',
            approved_by=actor if source == 'manual' else None,
            approved_at=now if source == 'manual' else None,
        ).returning(memories))).mappings().one()
        await self._memory_audit(session, row['id'], actor, 'create')
        if source == 'manual':
            await self._store_memory_vector(session, dict(row), vector)
        return dict(row)

    async def create_memory(self, body: dict, actor: str,
                            vector: list[float] | None = None) -> dict:
        if self.tenant_id is None:
            raise ValueError('memories require a tenant')
        async with self._transaction() as session:
            return await self._create_memory(session, body, actor, 'manual', vector=vector)

    async def propose_memories(self, task_id: str, source: str, candidates: list[dict],
                               batch_key: str | None = None) -> list:
        from sca.db.schema import memories
        from sca.memory.models import normalise_scope

        if self.tenant_id is None or source not in ('agent_proposal', 'reviewer_feedback'):
            raise ValueError('invalid proposal source or tenant')
        async with self._transaction() as session:
            task = await self._task(session, task_id, lock=True)
            key = 'memory_' + source + (':' + batch_key if batch_key else '')
            if key in task['result']:
                return task['result'][key]
            scope = ('repo:' + task['spec']['repo_url'] if task['spec'].get('repo_url')
                     else 'kind:' + task['kind'])
            ids = []
            for candidate in candidates[:3]:
                existing = await session.scalar(select(memories.c.id).where(
                    memories.c.tenant_id == self.tenant_id,
                    memories.c.source_task_id == task['id'], memories.c.source == source,
                    memories.c.text == candidate['text'],
                ))
                if existing is None:
                    row = await self._create_memory(
                        session, {**candidate, 'scope': normalise_scope(scope)},
                        'system:' + source, source, task['id'])
                    existing = row['id']
                ids.append(str(existing))
            result = {**task['result'], key: ids,
                      'memory_proposals': list(dict.fromkeys(
                          [*task['result'].get('memory_proposals', []), *ids]))}
            await session.execute(update(tasks).where(tasks.c.id == task['id']).values(result=result))
            return ids

    async def change_memory(self, memory_id: UUID, actor: str, changes: dict,
                            source_task_id: UUID | None = None,
                            vector: list[float] | None = None,
                            vector_text: str | None = None) -> dict:
        from sca.db.schema import memories
        from sca.memory.models import MemoryInput

        if self.tenant_id is None:
            raise ValueError('memories require a tenant')
        async with self._transaction() as session:
            row = (await session.execute(select(memories).where(
                memories.c.id == memory_id, memories.c.tenant_id == self.tenant_id,
            ).with_for_update())).mappings().first()
            if row is None or (source_task_id is not None and row['source_task_id'] != source_task_id):
                raise NotFoundError('Memory not found')
            values = MemoryInput.model_validate({**row, **changes}).model_dump()
            status = changes.get('status', row['status'])
            if status not in ('active', 'rejected', 'retired', 'proposed'):
                raise ValueError('invalid memory status')
            if status != row['status'] and (row['status'], status) not in (
                ('proposed', 'active'), ('proposed', 'rejected'), ('active', 'retired'),
            ):
                raise ConflictError('Invalid memory status transition')
            values.update(status=status, updated_at=datetime.now(UTC))
            action = {'active': 'approve', 'rejected': 'reject', 'retired': 'retire'}.get(status)
            if status == 'active' and row['status'] == 'proposed':
                values.update(approved_by=actor, approved_at=datetime.now(UTC))
            changed = (await session.execute(update(memories).where(
                memories.c.id == memory_id, memories.c.tenant_id == self.tenant_id,
            ).values(**values).returning(memories))).mappings().one()
            await self._memory_audit(session, memory_id, actor,
                                     action if status != row['status'] else 'update')
            if await self._has_memory_vectors(session) and values['text'] != row['text']:
                await session.execute(text("""UPDATE memories SET embedding = NULL
                    WHERE id = :id AND tenant_id = :tenant"""),
                    {'id': memory_id, 'tenant': self.tenant_id})
            if (status == 'active' and (row['status'] != 'active' or values['text'] != row['text'])
                    and (vector_text is None or vector_text == values['text'])):
                await self._store_memory_vector(session, dict(changed), vector)
            return dict(changed)

    async def delete_memory(self, memory_id: UUID, actor: str) -> None:
        from sqlalchemy import delete

        from sca.db.schema import memories

        async with self._transaction() as session:
            deleted = await session.scalar(delete(memories).where(
                memories.c.id == memory_id, memories.c.tenant_id == self.tenant_id,
            ).returning(memories.c.id))
            if deleted is None:
                raise NotFoundError('Memory not found')
            await self._memory_audit(session, memory_id, actor, 'delete')

    async def memory_query(self, task_id: str, max_items: int, max_chars: int) -> str | None:
        from sca.db.schema import memories
        from sca.memory.models import normalise_scope, render

        async with self._transaction() as session:
            task = await self._task(session, task_id)
            if 'memory_context' in task['result']:
                return None
            scopes = ['tenant', 'kind:' + task['kind']]
            if task['spec'].get('repo_url'):
                scopes.append(normalise_scope('repo:' + task['spec']['repo_url']))
            conditions = (
                memories.c.tenant_id == self.tenant_id, memories.c.scope.in_(scopes),
                memories.c.status == 'active',
            )
            count = await session.scalar(select(func.count()).select_from(memories).where(*conditions))
            if count <= max_items:
                rows = (await session.execute(select(memories).where(*conditions).order_by(
                    memories.c.last_used_at.desc().nullslast(),
                    memories.c.created_at.desc(), memories.c.id,
                ))).mappings().all()
                if len(render([dict(r) for r in rows], max_items, max_chars)[1]) == count:
                    return None
            if not await self._has_memory_vectors(session):
                return None
            present = await session.scalar(text("""SELECT EXISTS (SELECT 1 FROM memories
                WHERE tenant_id = :tenant AND scope = ANY(:scopes) AND status = 'active'
                AND embedding IS NOT NULL)"""),
                {'tenant': self.tenant_id, 'scopes': scopes})
            if not present:
                return None
            return task['spec'].get('instruction') or task['spec'].get('question', '')

    async def inject_memories(self, task_id: str, max_items: int, max_chars: int, *,
                              settings: Settings | None = None,
                              vector: list[float] | None = None) -> str:
        from sca.db.schema import memories
        from sca.memory.models import normalise_scope, render

        if self.tenant_id is None:
            raise ValueError('memories require a tenant')
        async with self._transaction() as session:
            task = await self._task(session, task_id, lock=True)
            if 'memory_context' in task['result']:
                return task['result']['memory_context']
            scopes = ['tenant', 'kind:' + task['kind']]
            if task['spec'].get('repo_url'):
                scopes.append(normalise_scope('repo:' + task['spec']['repo_url']))
            rows = (await session.execute(select(memories).where(
                memories.c.tenant_id == self.tenant_id, memories.c.scope.in_(scopes),
                memories.c.status == 'active',
            ).order_by(memories.c.last_used_at.desc().nullslast(),
                       memories.c.created_at.desc(), memories.c.id))).mappings().all()
            from sca.config import get_settings
            from sca.memory.embedding import literal

            settings = settings or get_settings()
            ordered = [dict(row) for row in rows]
            _, all_ids = render(ordered, max_items, max_chars)
            method = 'all' if len(all_ids) == len(rows) else 'recent'
            if (method == 'recent' and vector is not None
                    and await self._has_memory_vectors(session)):
                hits = (await session.execute(text("""SELECT id FROM memories
                    WHERE tenant_id = :tenant AND id = ANY(:ids)
                    AND embedding IS NOT NULL AND vector_dims(embedding) = :dim
                    ORDER BY embedding <=> CAST(:v AS vector), id"""),
                    {'tenant': self.tenant_id, 'ids': [r['id'] for r in rows],
                     'dim': len(vector), 'v': literal(vector)})).scalars().all()
                if hits:
                    by_id = {r['id']: r for r in ordered}
                    ranked = [by_id[i] for i in hits]
                    ranked += [r for r in ordered if r['id'] not in set(hits)]
                    limit = min(max_items, settings.memory_retrieval_top_k)
                    reserved = []
                    for index in range(settings.memory_min_per_category):
                        for category in ('command', 'pitfall'):
                            candidates = [r for r in ranked if r['category'] == category]
                            if index < len(candidates):
                                reserved.append(candidates[index])
                    reserved_ids = {r['id'] for r in reserved}
                    ordered = (reserved + [r for r in ranked
                                           if r['id'] not in reserved_ids])[:limit]
                    method = 'vector'
            content, ids = render(ordered, max_items, max_chars)
            if ids:
                await session.execute(update(memories).where(
                    memories.c.tenant_id == self.tenant_id, memories.c.id.in_(ids),
                ).values(uses=memories.c.uses + 1, last_used_at=datetime.now(UTC)))
            chosen = set(ids)
            entries = [{"id": str(row['id']), "scope": row['scope'].partition(':')[0],
                        "category": row['category'], "text": row['text']}
                       for row in ordered if row['id'] in chosen]
            await self._event(session, task['id'], 'memory.injected',
                              {'ids': [str(i) for i in ids], 'method': method, 'entries': entries},
                              dedup_key='memory.injected')
            await session.execute(update(tasks).where(tasks.c.id == task['id']).values(
                result={**task['result'], 'memory_context': content}))
            return content

    async def accept_memories(self, task_id: UUID, ids: list[UUID], actor: str,
                              vectors: dict[UUID, list[float] | None] | None = None) -> None:
        from sca.db.schema import memories

        if not ids:
            return
        if self.tenant_id is None:
            raise ValueError('memories require a tenant')
        async with self._transaction() as session:
            task = await self._task(session, task_id, lock=True)
            if task['state'] != TaskState.AWAITING_REVIEW:
                raise ConflictError('Task is not awaiting review')
            rows = (await session.execute(select(memories).where(
                memories.c.id.in_(ids), memories.c.tenant_id == self.tenant_id,
                memories.c.source_task_id == task_id,
            ).with_for_update())).mappings().all()
            if len(rows) != len(set(ids)):
                raise NotFoundError('Memory not found')
            if any(row['status'] not in ('proposed', 'active') for row in rows):
                raise ConflictError('Memory is no longer proposed')
            for row in rows:
                if row['status'] == 'active':
                    continue
                await session.execute(update(memories).where(
                    memories.c.id == row['id'], memories.c.tenant_id == self.tenant_id,
                ).values(status='active', approved_by=actor,
                         approved_at=datetime.now(UTC), updated_at=datetime.now(UTC)))
                await self._memory_audit(session, row['id'], actor, 'approve')
                await self._store_memory_vector(session, dict(row), (vectors or {}).get(row['id']))

    async def create_eval_run(self, run: dict) -> None:
        from sca.db.schema import eval_runs

        async with self._transaction() as session:
            await session.execute(pg_insert(eval_runs).values(**run).on_conflict_do_nothing())

    async def start_eval_run(self, run_id: str) -> None:
        from sca.db.schema import eval_runs

        async with self._transaction() as session:
            await session.execute(update(eval_runs).where(
                eval_runs.c.id == task_uuid(run_id), eval_runs.c.tenant_id == 'eval',
                eval_runs.c.status == 'queued',
            ).values(status='running'))

    async def finish_eval_run(self, run_id: str, status: str) -> None:
        from sca.db.schema import eval_runs

        async with self._transaction() as session:
            await session.execute(update(eval_runs).where(
                eval_runs.c.id == task_uuid(run_id), eval_runs.c.tenant_id == 'eval',
            ).values(status=status, finished_at=func.now()))

    async def record_eval_result(self, result: dict) -> None:
        from sca.db.schema import eval_results

        async with self._transaction() as session:
            await session.execute(pg_insert(eval_results).values(**result).on_conflict_do_nothing())

    async def update_eval_trajectory(self, run_id: str, case_id: str, repeat: int,
                                     passed: bool, findings: list[dict]) -> None:
        from sca.db.schema import eval_results

        async with self._transaction() as session:
            await session.execute(update(eval_results).where(
                eval_results.c.run_id == task_uuid(run_id), eval_results.c.tenant_id == "eval",
                eval_results.c.case_id == case_id, eval_results.c.repeat == repeat,
            ).values(trajectory_passed=passed, trajectory_findings=findings))

    async def get_eval_run(self, run_id: str) -> dict:
        from sca.db.schema import eval_results, eval_runs

        async with self._transaction() as session:
            run = (await session.execute(select(eval_runs).where(
                eval_runs.c.id == task_uuid(run_id), eval_runs.c.tenant_id == 'eval',
            ))).mappings().one()
            results = (await session.execute(select(eval_results).where(
                eval_results.c.run_id == task_uuid(run_id), eval_results.c.tenant_id == 'eval',
            ).order_by(eval_results.c.case_id, eval_results.c.repeat))).mappings().all()
            from sca.eval.trajectory import overall_passed

            return {**run, 'results': [{**row, 'passed': overall_passed(dict(row))} for row in results]}

    async def complete_acceptance(self, task_id: str, passed: bool,
                                  exit_code: int, output_tail: str) -> None:
        async with self._transaction() as session:
            row = await self._task(session, task_id, True)
            if 'acceptance_passed' in row['result']:
                return
            await session.execute(update(tasks).where(tasks.c.id == row['id']).values(
                result={**row['result'], 'acceptance_passed': passed}, updated_at=func.now()))
            await self._event(session, row['id'], 'acceptance.completed', {
                'passed': passed, 'exit_code': exit_code, 'output_tail': output_tail,
            }, 'acceptance.completed')

    async def create_evolution(self, values: dict) -> None:
        from sca.db.schema import evolutions

        if self.tenant_id is None:
            raise ValueError('tenant required')
        async with self._transaction() as session:
            await session.execute(pg_insert(evolutions).values(
                **values, tenant_id=self.tenant_id).on_conflict_do_nothing())

    async def list_evolutions(self) -> list[dict]:
        from sca.db.schema import evolutions

        async with self._transaction() as session:
            return [dict(r) for r in (await session.execute(select(evolutions).where(
                evolutions.c.tenant_id == self.tenant_id).order_by(
                    evolutions.c.created_at.desc()).limit(100))).mappings()]

    async def get_evolution(self, evolution_id: str) -> dict:
        from sca.db.schema import eval_results, eval_runs, evolution_candidates, evolutions

        async with self._transaction() as session:
            row = (await session.execute(select(evolutions).where(
                evolutions.c.id == task_uuid(evolution_id),
                evolutions.c.tenant_id == self.tenant_id))).mappings().one()
            candidates = [dict(r) for r in (await session.execute(select(evolution_candidates).where(
                evolution_candidates.c.evolution_id == row['id'],
                evolution_candidates.c.tenant_id == self.tenant_id).order_by(
                    evolution_candidates.c.generation, evolution_candidates.c.created_at))).mappings()]
            journal = [{k: c[k] for k in ('id', 'generation', 'hypothesis', 'decision', 'reason',
                                         'dev_metrics', 'holdout_metrics')} for c in candidates]
            runs = (await session.execute(select(eval_runs).where(
                eval_runs.c.tenant_id == self.tenant_id,
                eval_runs.c.variant['evolution_id'].astext == str(row['id'])
            ).order_by(eval_runs.c.created_at))).mappings().all()
            for run in runs:
                cost = (await session.execute(select(func.coalesce(func.sum(eval_results.c.cost_usd), 0)).where(
                    eval_results.c.run_id == run['id'], eval_results.c.tenant_id == self.tenant_id))).scalar_one()
                journal.append({'type': 'evaluation', 'id': run['id'], 'status': run['status'],
                                'split': run['variant'].get('split'), 'cost_usd': float(cost),
                                'overlay': run['variant'].get('variant', {}).get('agent_overlay', {})})
            return {**row, 'candidates': candidates, 'journal': journal}

    async def update_evolution(self, evolution_id: str, values: dict) -> None:
        from sca.db.schema import evolutions

        if values.get('status') in ('completed', 'failed', 'stopped', 'budget_exhausted', 'patience_exhausted'):
            values = {'finished_at': datetime.now(UTC), **values}
        async with self._transaction() as session:
            await session.execute(update(evolutions).where(
                evolutions.c.id == task_uuid(evolution_id),
                evolutions.c.tenant_id == self.tenant_id).values(**values, updated_at=func.now()))

    async def save_evolution_candidate(self, values: dict) -> None:
        from sca.db.schema import evolution_candidates, evolutions

        async with self._transaction() as session:
            (await session.execute(select(evolutions.c.id).where(
                evolutions.c.id == values['evolution_id'],
                evolutions.c.tenant_id == self.tenant_id))).one()
            statement = pg_insert(evolution_candidates).values(**values, tenant_id=self.tenant_id)
            await session.execute(statement.on_conflict_do_update(
                index_elements=['id'], set_={k: v for k, v in values.items() if k != 'id'},
                where=evolution_candidates.c.tenant_id == self.tenant_id))

    async def list_champions(self, agent_version: str | None = None,
                             model: str | None = None) -> list[dict]:
        from sca.db.schema import champions

        query = select(champions).where(champions.c.tenant_id == self.tenant_id)
        if agent_version is not None:
            query = query.where(champions.c.agent_version == agent_version)
        if model is not None:
            query = query.where(champions.c.model == model)
        async with self._transaction() as session:
            return [dict(r) for r in (await session.execute(query.order_by(
                champions.c.created_at.desc()))).mappings()]

    async def activate_champion(self, champion_id: str, values: dict | None = None) -> dict:
        from sca.db.schema import champions

        async with self._transaction() as session:
            if values is None:
                target = dict((await session.execute(select(champions).where(
                    champions.c.id == task_uuid(champion_id),
                    champions.c.tenant_id == self.tenant_id))).mappings().one())
            else:
                target = {**values, 'id': task_uuid(champion_id), 'tenant_id': self.tenant_id}
            # Serialize even the first promotion, where there is no active row to lock.
            lock_key = f"{self.tenant_id}:{target['agent_version']}:{target['model']}"
            await session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                                  {'key': lock_key})
            await session.execute(update(champions).where(
                champions.c.tenant_id == self.tenant_id,
                champions.c.agent_version == target['agent_version'],
                champions.c.model == target['model'], champions.c.active).values(active=False))
            if values is not None:
                await session.execute(pg_insert(champions).values(**target, active=False).on_conflict_do_nothing())
            return dict((await session.execute(update(champions).where(
                champions.c.id == task_uuid(champion_id), champions.c.tenant_id == self.tenant_id
            ).values(active=True).returning(champions))).mappings().one())

    async def get_active_champion(self, agent_version: str, model: str) -> dict | None:
        rows = await self.list_champions(agent_version, model)
        return next((row for row in rows if row['active']), None)

    async def evolution_evidence(self, task_id: str) -> list[dict]:
        async with self._transaction() as session:
            row = await self._task(session, task_id, True)
            found = (await session.execute(select(events.c.payload).where(
                events.c.task_id == row['id'], events.c.tenant_id == self.tenant_id,
                events.c.type.in_(('agent.command', 'acceptance.completed'))
            ).order_by(events.c.seq.desc()).limit(8))).scalars().all()
            return [{k: str(payload[k])[-1000:] for k in ('command', 'output_tail') if k in payload}
                    for payload in reversed(found)]
