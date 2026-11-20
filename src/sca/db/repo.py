from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
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
            stmt = pg_insert(tenants).values(
                id=tenant_id, display_name=tenant_id, max_active_tasks=max_active_tasks,
            )
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
        initial_result: dict | None = None,  # SCM issue link is committed with the task.
    ) -> tuple[Task, bool]:
        async with self._transaction() as session:
            if self.tenant_id is not None and tenant_id != self.tenant_id:
                raise NotFoundError("Tenant not found")
            await session.execute(
                pg_insert(tenants)
                .values(id=tenant_id, display_name=tenant_id,
                        max_active_tasks=self.default_max_active_tasks)
                .on_conflict_do_nothing(index_elements=[tenants.c.id])
            )
            # Commercial quotas share the tenant lock with submission and suspension.
            tenant = await self._find_tenant(session, tenant_id, lock=True)
            if tenant is None:
                raise NotFoundError("租户不存在")
            self._require_active_tenant(tenant)
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
            spec = await self._apply_plan(session, tenant, spec)
            stmt = pg_insert(tasks).values(
                id=uuid4(),
                tenant_id=tenant_id,
                kind=kind,
                spec=spec,
                state=TaskState.QUEUED,
                idempotency_key=idempotency_key,
                result=initial_result or {},
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

        from sca.admission.order import grant_order, tenant_slot_limit
        from sca.db.schema import plans

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
            quotas = (await session.execute(
                select(tenants, plans.c.max_concurrent_tasks)
                .join(plans, tenants.c.plan == plans.c.name)
            )).mappings().all()
            weights = {r["id"]: r["weight"] for r in quotas}
            limits = {r["id"]: tenant_slot_limit(r["max_active_tasks"], r["max_concurrent_tasks"])
                      for r in quotas}
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
            # Usage is committed with the terminal state and can be adjusted by late events.
            if new_state in TERMINAL_STATES:
                await self._meter_finished_task(session, updated)
            return Task.model_validate(dict(updated))

    async def append_event(
        self, task_id: str | UUID, type: str, payload: dict, dedup_key: str | None = None
    ) -> Event | None:
        from sca.billing.metering import METERED_EVENTS

        async with self._transaction() as session:
            row = await self._task(session, task_id, True)
            event = await self._event(session, row["id"], type, payload, dedup_key)
            terminal = row["state"] in TERMINAL_STATES
            if terminal and event is not None and type in METERED_EVENTS:
                await self._meter_finished_task(session, row)
        if (terminal and type == "sandbox.destroyed" and "subtask" not in payload
                and "best_of_candidate" not in payload):
            from sca.config import get_settings
            from sca.maintenance import cleanup_finished_cache

            await cleanup_finished_cache(get_settings(), self, row["id"])
        return event

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
            if column == "usage" and row["state"] in TERMINAL_STATES:
                await self._meter_finished_task(session, {**row, "usage": value})

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
                display_name=tenant_id,
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

    async def activate_champion(
        self, champion_id: str, values: dict | None = None, *,
        rollout: tuple[int, str] | None = None,
    ) -> dict:
        from sca.db.schema import champions

        async with self._transaction() as session:
            if values is None:
                target = dict((await session.execute(select(champions).where(
                    champions.c.id == task_uuid(champion_id),
                    champions.c.tenant_id == self.tenant_id))).mappings().one())
            else:
                target = {**values, 'id': task_uuid(champion_id), 'tenant_id': self.tenant_id}
            await self._lock_champion(session, self.tenant_id, target)
            if values is not None:
                inserted = await session.scalar(pg_insert(champions).values(
                    **target, active=False).on_conflict_do_nothing().returning(champions.c.id))
                if inserted is None:
                    # A retry of a committed promotion must leave subsequent rollbacks intact.
                    return dict((await session.execute(select(champions).where(
                        champions.c.id == target['id'],
                        champions.c.tenant_id == self.tenant_id))).mappings().one())
            row = await self._switch_active_champion(session, target)
            if rollout is not None:
                percent, actor = rollout
                await self._write_champion_rollout(session, row, percent, actor)
            return row

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

    # Evolution productization

    async def get_champion(self, champion_id: UUID | str) -> dict | None:
        from sca.db.schema import champions

        async with self._transaction() as session:
            row = (await session.execute(select(champions).where(
                champions.c.id == task_uuid(champion_id),
                champions.c.tenant_id == self.tenant_id))).mappings().first()
            return dict(row) if row is not None else None

    async def pin_task_champion(self, task_id: str, selection: dict | None) -> None:
        async with self._transaction() as session:
            task = await self._task(session, task_id, lock=True)
            if "champion_id" in task["result"]:
                return
            chosen = selection or {"champion_id": None}
            await session.execute(update(tasks).where(tasks.c.id == task["id"]).values(
                result={**task["result"], **chosen}, updated_at=func.now()))
            if selection is not None:
                await self._event(session, task["id"], "champion.applied", selection, "champion.applied")

    async def get_task_champion(self, task_id: str) -> dict | None:
        champion_id = (await self.get_task(task_id)).result.get("champion_id")
        if champion_id is None:
            return None
        champion = await self.for_tenant("eval").get_champion(champion_id)
        if champion is None:
            raise NotFoundError("Pinned champion not found")
        return champion

    @staticmethod
    async def _lock_champion(session: AsyncSession, tenant_id: str | None, target: dict) -> None:
        # Serialize changes by version and model, including the first promotion.
        key = f"{tenant_id}:{target['agent_version']}:{target['model']}"
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                              {"key": key})

    async def _switch_active_champion(self, session: AsyncSession, target: dict) -> dict:
        from sca.db.schema import champions

        await session.execute(update(champions).where(
            champions.c.tenant_id == self.tenant_id, champions.c.active,
            champions.c.agent_version == target["agent_version"], champions.c.model == target["model"],
            champions.c.id != target["id"]).values(active=False))
        return dict((await session.execute(update(champions).where(
            champions.c.id == target["id"], champions.c.tenant_id == self.tenant_id
        ).values(active=True).returning(champions))).mappings().one())

    async def list_champion_rollouts(self) -> list[dict]:
        from sca.db.schema import champion_rollouts

        c = champion_rollouts.c
        async with self._transaction() as session:
            return [dict(row) for row in (await session.execute(select(champion_rollouts).where(
                c.tenant_id == self.tenant_id).order_by(c.agent_version, c.model))).mappings()]

    async def get_champion_rollout(self, agent_version: str, model: str) -> dict | None:
        from sca.db.schema import champion_rollouts

        c = champion_rollouts.c
        async with self._transaction() as session:
            row = (await session.execute(select(champion_rollouts).where(
                c.tenant_id == self.tenant_id, c.agent_version == agent_version,
                c.model == model))).mappings().first()
            return dict(row) if row is not None else None

    async def put_champion_rollout(
        self, agent_version: str, model: str, champion_id: UUID, percent: int, actor: str,
    ) -> dict:
        champion = await self.for_tenant("eval").get_champion(champion_id)
        if champion is None or (champion["agent_version"], champion["model"]) != (agent_version, model):
            raise NotFoundError("该版本和模型的冠军不存在")
        async with self._transaction() as session:
            await self._lock_champion(session, "eval", champion)
            return await self._write_champion_rollout(session, champion, percent, actor)

    async def _write_champion_rollout(
        self, session: AsyncSession, champion: dict, percent: int, actor: str,
    ) -> dict:
        from sca.db.schema import champion_rollouts, evolution_events

        c = champion_rollouts.c
        key = {"tenant_id": self.tenant_id, "agent_version": champion["agent_version"],
               "model": champion["model"]}
        values = {"champion_id": champion["id"], "percent": percent, "updated_by": actor}
        statement = pg_insert(champion_rollouts).values(**key, **values).on_conflict_do_update(
            index_elements=[c.tenant_id, c.agent_version, c.model],
            set_={**values, "updated_at": func.now()},
            where=(c.champion_id != champion["id"]) | (c.percent != percent))
        row = (await session.execute(statement.returning(champion_rollouts))).mappings().first()
        if row is None:
            return dict((await session.execute(select(champion_rollouts).filter_by(**key))).mappings().one())
        await session.execute(insert(evolution_events).values(
            tenant_id=self.tenant_id, type="rollout.updated", actor=actor, payload={
                "agent_version": key["agent_version"], "model": key["model"],
                "champion_id": str(champion["id"]), "percent": percent,
            }))
        return dict(row)

    async def rollback_champion(self, champion_id: UUID, actor: str) -> dict:
        from sca.db.schema import evolution_events

        if self.tenant_id != "eval":
            raise NotFoundError("冠军记录不存在")
        child = await self.get_champion(champion_id)
        if child is None:
            raise NotFoundError("冠军记录不存在")
        parent_id = child["parent_champion_id"]
        parent = await self.get_champion(parent_id) if parent_id is not None else None
        if parent is None or (parent["agent_version"], parent["model"]) != (
            child["agent_version"], child["model"]
        ):
            raise ConflictError("该冠军没有可回滚的父冠军")
        async with self._transaction() as session:
            await self._lock_champion(session, self.tenant_id, child)
            row = await self._switch_active_champion(session, parent)
            count = await session.scalar(text(
                "SELECT retarget_champion_rollouts(:champion, :actor)"
            ), {"champion": champion_id, "actor": actor})
            await session.execute(insert(evolution_events).values(
                tenant_id=self.tenant_id, type="champion.rolled_back", actor=actor, payload={
                    "previous_champion_id": str(champion_id), "champion_id": str(parent_id),
                    "rollouts_updated": count,
                }))
            return row

    @staticmethod
    def _schedule_due(now: datetime) -> Any:
        from sca.db.schema import evolution_schedules

        c = evolution_schedules.c
        elapsed_hours = func.extract("epoch", now - c.last_run_at) / 3600
        return c.enabled & (c.last_run_at.is_(None) | (elapsed_hours >= c.interval_hours))

    async def due_evolution_schedules(self, now: datetime) -> list[UUID]:
        from sca.db.schema import evolution_schedules

        c = evolution_schedules.c
        async with self._transaction() as session:
            return list((await session.scalars(select(c.id).where(
                c.tenant_id == self.tenant_id, self._schedule_due(now)).order_by(c.id))).all())

    async def create_evolution_schedule(self, values: dict) -> dict:
        from sca.db.schema import evolution_schedules

        async with self._transaction() as session:
            row = (await session.execute(insert(evolution_schedules).values(
                **values, id=uuid4(), tenant_id=self.tenant_id,
            ).returning(evolution_schedules))).mappings().one()
            return dict(row)

    async def list_evolution_schedules(self) -> list[dict]:
        from sca.db.schema import evolution_schedules

        c = evolution_schedules.c
        async with self._transaction() as session:
            return [dict(row) for row in (await session.execute(select(evolution_schedules).where(
                c.tenant_id == self.tenant_id).order_by(c.suite, c.id))).mappings()]

    async def get_evolution_schedule(self, schedule_id: UUID) -> dict:
        from sca.db.schema import evolution_schedules

        c = evolution_schedules.c
        async with self._transaction() as session:
            row = (await session.execute(select(evolution_schedules).where(
                c.id == schedule_id, c.tenant_id == self.tenant_id))).mappings().first()
            if row is None:
                raise NotFoundError("演进计划不存在")
            return dict(row)

    async def update_evolution_schedule(self, schedule_id: UUID, values: dict) -> dict:
        from sca.db.schema import evolution_schedules

        c = evolution_schedules.c
        async with self._transaction() as session:
            row = (await session.execute(update(evolution_schedules).where(
                c.id == schedule_id, c.tenant_id == self.tenant_id
            ).values(**values).returning(evolution_schedules))).mappings().first()
            if row is None:
                raise NotFoundError("演进计划不存在")
            return dict(row)

    async def delete_evolution_schedule(self, schedule_id: UUID) -> None:
        from sqlalchemy import delete

        from sca.db.schema import evolution_schedules

        c = evolution_schedules.c
        async with self._transaction() as session:
            deleted = await session.scalar(delete(evolution_schedules).where(
                c.id == schedule_id, c.tenant_id == self.tenant_id).returning(c.id))
            if deleted is None:
                raise NotFoundError("演进计划不存在")

    async def claim_evolution_schedule(self, schedule_id: UUID, now: datetime, force: bool) -> dict | None:
        from sca.db.schema import evolution_schedules

        c = evolution_schedules.c
        stmt = select(evolution_schedules).where(c.id == schedule_id, c.tenant_id == self.tenant_id)
        if not force:
            stmt = stmt.where((c.last_run_at == now) | self._schedule_due(now))
        async with self._transaction() as session:
            row = (await session.execute(stmt.with_for_update())).mappings().first()
            if row is None:
                return None
            await session.execute(update(evolution_schedules).where(
                c.id == schedule_id, c.tenant_id == self.tenant_id).values(last_run_at=now))
            return dict(row)

    async def record_evolution_schedule_run(
        self, schedule_id: UUID, run_id: UUID, evolution: dict | None,
    ) -> bool:
        from sca.db.schema import evolution_schedules, evolutions

        c = evolution_schedules.c
        async with self._transaction() as session:
            conditions = (c.id == schedule_id, c.tenant_id == self.tenant_id)
            if await session.scalar(select(c.id).where(*conditions).with_for_update()) is None:
                return False
            values = {"last_eval_run_id": run_id}
            if evolution is not None:
                await session.execute(pg_insert(evolutions).values(
                    **evolution, tenant_id=self.tenant_id).on_conflict_do_nothing())
                values["last_evolution_id"] = evolution["id"]
            await session.execute(update(evolution_schedules).where(*conditions).values(**values))
            return True

    async def create_harvest_candidate(self, values: dict) -> dict:
        from sca.db.schema import harvest_candidates

        async with self._transaction() as session:
            await self._task(session, values["task_id"])
            row = (await session.execute(insert(harvest_candidates).values(
                **values, tenant_id=self.tenant_id).returning(harvest_candidates))).mappings().one()
            return dict(row)

    async def list_harvest_candidates(self) -> list[dict]:
        from sca.db.schema import harvest_candidates

        c = harvest_candidates.c
        async with self._transaction() as session:
            return [dict(row) for row in (await session.execute(select(harvest_candidates).where(
                c.tenant_id == self.tenant_id).order_by(c.created_at.desc(), c.id))).mappings()]

    async def review_harvest_candidate(
        self, candidate_id: UUID, status: str, actor: str,
        publish: Callable[[dict], Awaitable[None]] | None = None,
    ) -> dict:
        from sca.db.schema import harvest_candidates

        c = harvest_candidates.c
        async with self._transaction() as session:
            conditions = (c.id == candidate_id, c.tenant_id == self.tenant_id)
            row = (await session.execute(select(harvest_candidates).where(
                *conditions).with_for_update())).mappings().first()
            if row is None:
                raise NotFoundError("采集候选不存在")
            if row["status"] == status:
                return dict(row)
            if row["status"] != "pending":
                raise ConflictError("采集候选已审核")
            if publish is not None:
                await publish(dict(row))
            return dict((await session.execute(update(harvest_candidates).where(*conditions).values(
                status=status, reviewed_by=actor, reviewed_at=func.now()
            ).returning(harvest_candidates))).mappings().one())
    # Tenant administration, plans and usage

    async def _find_tenant(
        self, session: AsyncSession, tenant_id: str, *, lock: bool = False,
    ) -> RowMapping | None:
        if self.tenant_id is not None and self.tenant_id != tenant_id:
            return None
        query = select(tenants).where(tenants.c.id == tenant_id)
        if lock:
            query = query.with_for_update(key_share=True)
        return (await session.execute(query)).mappings().first()

    @staticmethod
    def _tenant_view(row: Mapping[str, Any]) -> dict[str, Any]:
        return {key: row[key] for key in ("id", "display_name", "plan", "status", "created_at")}

    @staticmethod
    def _require_active_tenant(row: RowMapping | None) -> None:
        from sca.tenancy.models import TenantSuspendedError

        if row is not None and (row["status"] == "suspended" or row["deleted_at"] is not None):
            raise TenantSuspendedError()

    async def check_tenant_submission(self, tenant_id: str) -> None:
        scoped = self.for_tenant(tenant_id)
        async with scoped._transaction() as session:
            self._require_active_tenant(await scoped._find_tenant(session, tenant_id))

    async def tenant_deleted(self, tenant_id: str) -> bool:
        scoped = self.for_tenant(tenant_id)
        async with scoped._transaction() as session:
            row = await scoped._find_tenant(session, tenant_id)
            return row is not None and row["deleted_at"] is not None

    async def list_tenants(self) -> list[dict[str, Any]]:
        if self.tenant_id is not None:
            raise ValueError("Platform scope required")
        async with self._transaction() as session:
            rows = (await session.execute(select(tenants).where(
                tenants.c.deleted_at.is_(None)).order_by(tenants.c.id))).mappings()
            return [self._tenant_view(row) for row in rows]

    async def get_tenant(self, tenant_id: str) -> dict[str, Any]:
        async with self._transaction() as session:
            row = await self._find_tenant(session, tenant_id)
            if row is None or row["deleted_at"] is not None:
                raise NotFoundError("租户不存在")
            return self._tenant_view(row)

    async def _plan(self, session: AsyncSession, name: str) -> RowMapping:
        from sca.db.schema import plans

        row = (await session.execute(select(plans).where(plans.c.name == name))).mappings().first()
        if row is None:
            raise NotFoundError("套餐不存在")
        return row

    async def list_plans(self) -> list[dict[str, Any]]:
        from sca.db.schema import plans

        async with self._transaction() as session:
            rows = (await session.execute(select(plans).order_by(plans.c.name))).mappings()
            return [dict(row) for row in rows]

    async def create_tenant(
        self, tenant_id: str, display_name: str, plan: str, owner_email: str | None,
    ) -> dict[str, Any]:
        from sca.db.schema import tenant_members

        if self.tenant_id is not None:
            raise ValueError("Platform scope required")
        async with self._transaction() as session:
            await self._plan(session, plan)
            row = (await session.execute(pg_insert(tenants).values(
                id=tenant_id, display_name=display_name, plan=plan, membership_required=True,
                max_active_tasks=self.default_max_active_tasks,
            ).on_conflict_do_nothing(index_elements=[tenants.c.id]).returning(tenants)
            )).mappings().first()
            if row is None:
                raise ConflictError("租户标识已存在")
            if owner_email is not None:
                await session.execute(insert(tenant_members).values(
                    tenant_id=tenant_id, email=owner_email, role="owner"))
            return self._tenant_view(row)

    async def update_tenant(self, tenant_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {"display_name"} if self.tenant_id is not None else {"display_name", "plan", "status"}
        if changes.keys() - allowed or (self.tenant_id is not None and tenant_id != self.tenant_id):
            raise ValueError("Invalid tenant update")
        if not changes:
            return await self.get_tenant(tenant_id)
        async with self._transaction() as session:
            if "plan" in changes:
                await self._plan(session, changes["plan"])
            row = (await session.execute(update(tenants).where(
                tenants.c.id == tenant_id, tenants.c.deleted_at.is_(None),
            ).values(**changes).returning(tenants))).mappings().first()
            if row is None:
                raise NotFoundError("租户不存在")
            return self._tenant_view(row)

    async def delete_tenant(self, tenant_id: str) -> None:
        if self.tenant_id is not None:
            raise ValueError("Platform scope required")
        async with self._transaction() as session:
            deleted = await session.scalar(update(tenants).where(
                tenants.c.id == tenant_id, tenants.c.deleted_at.is_(None),
            ).values(status="suspended", deleted_at=func.now()).returning(tenants.c.id))
            if deleted is None:
                raise NotFoundError("租户不存在")
            await session.execute(update(api_tokens).where(
                api_tokens.c.tenant_id == tenant_id, api_tokens.c.revoked_at.is_(None),
            ).values(revoked_at=func.now()))

    @staticmethod
    def _member_view(row: RowMapping) -> dict[str, Any]:
        return {**row, "status": "active" if row["subject"] is not None else "pending"}

    async def list_members(self) -> list[dict[str, Any]]:
        from sca.db.schema import tenant_members

        if self.tenant_id is None:
            raise ValueError("Tenant scope required")
        async with self._transaction() as session:
            rows = (await session.execute(select(tenant_members).where(
                tenant_members.c.tenant_id == self.tenant_id,
            ).order_by(tenant_members.c.email))).mappings()
            return [self._member_view(row) for row in rows]

    async def invite_member(self, email: str, role: str) -> dict[str, Any]:
        from sca.db.schema import tenant_members

        if self.tenant_id is None:
            raise ValueError("Tenant scope required")
        async with self._transaction() as session:
            tenant = await self._find_tenant(session, self.tenant_id, lock=True)
            if tenant is None or tenant["deleted_at"] is not None:
                raise NotFoundError("租户不存在")
            row = (await session.execute(pg_insert(tenant_members).values(
                tenant_id=self.tenant_id, email=email, role=role,
            ).on_conflict_do_nothing().returning(tenant_members))).mappings().first()
            if row is None:
                raise ConflictError("该邮箱已被邀请或已加入租户")
            return self._member_view(row)

    async def oidc_member_tenants(self, subject: str, email: str | None) -> list[str]:
        async with self._transaction() as session:
            return list(await session.scalars(text(
                "SELECT public.sca_oidc_member_tenants(:subject, :email)"
            ), {"subject": subject, "email": email}))

    async def resolve_oidc_membership(self, subject: str, email: str | None) -> tuple[bool, str | None]:
        from sca.db.schema import tenant_members

        if self.tenant_id is None:
            raise ValueError("Tenant scope required")
        member_scope = tenant_members.c.tenant_id == self.tenant_id
        active = select(tenant_members.c.role).where(member_scope, tenant_members.c.subject == subject)
        async with self._transaction() as session:
            tenant = await self._find_tenant(session, self.tenant_id)
            if tenant is None:
                return False, None
            if tenant["deleted_at"] is not None:
                return True, None
            role = await session.scalar(active)
            if role is None and email is not None:
                # Serialize first logins for the same subject even if the provider changed its email.
                tenant = await self._find_tenant(session, self.tenant_id, lock=True)
                if tenant is None or tenant["deleted_at"] is not None:
                    return True, None
                role = await session.scalar(active)
                if role is None:
                    role = await session.scalar(update(tenant_members).where(
                        member_scope, tenant_members.c.email == email,
                        tenant_members.c.subject.is_(None),
                    ).values(subject=subject, activated_at=func.now()).returning(tenant_members.c.role))
            return tenant["membership_required"], role

    async def _apply_plan(self, session: AsyncSession, tenant: RowMapping, spec: dict) -> dict:
        from sca.billing.quota import apply_cost_limits
        from sca.db.schema import usage_daily

        plan = await self._plan(session, tenant["plan"])
        monthly = plan["monthly_cost_limit_usd"]
        spent = Decimal(0)
        if monthly is not None:
            today = datetime.now(UTC).date()
            spent = await session.scalar(
                select(func.coalesce(func.sum(usage_daily.c.cost_usd), 0)).where(
                    usage_daily.c.tenant_id == tenant["id"],
                    usage_daily.c.day.between(today.replace(day=1), today),
                )
            )
        return apply_cost_limits(spec, plan["max_task_cost_usd"], monthly, spent)

    async def usage_rows(self, start: date, end: date) -> list[dict[str, Any]]:
        from sca.db.schema import usage_daily

        query = select(usage_daily).where(usage_daily.c.day.between(start, end))
        if self.tenant_id is not None:
            query = query.where(usage_daily.c.tenant_id == self.tenant_id)
        async with self._transaction() as session:
            rows = (await session.execute(query.order_by(
                usage_daily.c.tenant_id, usage_daily.c.day))).mappings()
            return [dict(row) for row in rows]

    async def _meter_finished_task(
        self, session: AsyncSession, task: Mapping[str, Any],
    ) -> None:
        from sca.billing.metering import METERED_EVENTS, completion_time, daily_delta, task_usage
        from sca.config import get_settings
        from sca.db.schema import usage_daily

        relevant = (*METERED_EVENTS, "task.state_changed")
        if task["events_archived_at"] is None:
            rows = (await session.execute(select(events).where(
                events.c.task_id == task["id"], events.c.tenant_id == task["tenant_id"],
                events.c.type.in_(relevant),
            ).order_by(events.c.seq))).mappings()
            history = [Event.model_validate(dict(row)) for row in rows]
        else:
            from sca.archive import read_events

            if self.objects is None:
                raise RuntimeError("Archived usage backfill requires an object store")
            history = [event for event in await read_events(self.objects, task["id"])
                       if event.type in relevant]

        finished_at = completion_time(task, history)
        day = finished_at.astimezone(UTC).date()
        before = task["billing_usage"] or {}
        after = task_usage(task, history, finished_at, get_settings().default_model)
        if before == after:
            return
        await session.execute(pg_insert(usage_daily).values(
            tenant_id=task["tenant_id"], day=day,
        ).on_conflict_do_nothing(index_elements=[usage_daily.c.tenant_id, usage_daily.c.day]))
        day_filter = (usage_daily.c.tenant_id == task["tenant_id"], usage_daily.c.day == day)
        row = (await session.execute(select(usage_daily).where(
            *day_filter).with_for_update())).mappings().one()
        await session.execute(update(usage_daily).where(*day_filter).values(
            **daily_delta(row, before, after)))
        await session.execute(update(tasks).where(tasks.c.id == task["id"]).values(
            finished_at=finished_at, billing_usage=after))

    async def usage_backfill(self) -> int:
        if self.tenant_id is not None:
            raise ValueError("Platform scope required")
        last_id, processed = None, 0
        while True:
            query = select(tasks.c.id).where(tasks.c.state.in_(TERMINAL_STATES))
            if last_id is not None:
                query = query.where(tasks.c.id > last_id)
            async with self._transaction() as session:
                batch = list(await session.scalars(query.order_by(tasks.c.id).limit(100)))
            if not batch:
                return processed
            for task_id in batch:
                async with self._transaction() as session:
                    row = await self._task(session, task_id, True)
                    await self._meter_finished_task(session, row)
                processed += 1
            last_id = batch[-1]
    # SCM
    async def create_scm_integration(self, values: dict) -> dict:
        from sca.db.schema import scm_integrations

        if self.tenant_id is None:
            raise ValueError("SCM integration requires a tenant")
        async with self._transaction() as session:
            stmt = pg_insert(scm_integrations).values(id=uuid4(), tenant_id=self.tenant_id, **values)
            row = (await session.execute(
                stmt.on_conflict_do_nothing().returning(scm_integrations)
            )).mappings().one_or_none()
            if row is None:
                raise ConflictError("仓库或 webhook 密钥引用已用于该租户的 SCM 集成")
            return dict(row)

    async def list_scm_integrations(self) -> list[dict]:
        from sca.db.schema import scm_integrations

        async with self._transaction() as session:
            rows = await session.execute(select(scm_integrations).where(
                scm_integrations.c.tenant_id == self.tenant_id,
            ).order_by(scm_integrations.c.created_at, scm_integrations.c.id))
            return [dict(row) for row in rows.mappings()]

    async def get_scm_integration(self, integration_id: UUID) -> dict:
        from sca.db.schema import scm_integrations

        async with self._transaction() as session:
            row = (await session.execute(select(scm_integrations).where(
                scm_integrations.c.id == integration_id,
                scm_integrations.c.tenant_id == self.tenant_id,
            ))).mappings().one_or_none()
            if row is None:
                raise NotFoundError("SCM 集成不存在")
            return dict(row)

    async def replace_scm_integration(self, integration_id: UUID, values: dict) -> dict:
        from sqlalchemy.exc import IntegrityError

        from sca.db.schema import scm_integrations

        try:
            async with self._transaction() as session:
                stmt = update(scm_integrations).where(
                    scm_integrations.c.id == integration_id,
                    scm_integrations.c.tenant_id == self.tenant_id,
                ).values(**values, updated_at=func.now()).returning(scm_integrations)
                row = (await session.execute(stmt)).mappings().one_or_none()
                if row is None:
                    raise NotFoundError("SCM 集成不存在")
                return dict(row)
        except IntegrityError:
            raise ConflictError("仓库或 webhook 密钥引用已用于该租户的 SCM 集成") from None

    async def delete_scm_integration(self, integration_id: UUID) -> None:
        from sqlalchemy import delete

        from sca.db.schema import scm_integrations

        async with self._transaction() as session:
            deleted = await session.scalar(delete(scm_integrations).where(
                scm_integrations.c.id == integration_id,
                scm_integrations.c.tenant_id == self.tenant_id,
            ).returning(scm_integrations.c.id))
            if deleted is None:
                raise NotFoundError("SCM 集成不存在")

    async def find_scm_integration(
        self, provider: str, host: str, repo_full_name: str, secret_ref: str,
    ) -> dict | None:
        from sca.db.schema import scm_integrations

        async with self._transaction() as session:
            row = (await session.execute(select(scm_integrations).where(
                scm_integrations.c.tenant_id == self.tenant_id,
                scm_integrations.c.provider == provider,
                scm_integrations.c.host == host,
                scm_integrations.c.repo_full_name == repo_full_name,
                scm_integrations.c.webhook_secret_ref == secret_ref,
                scm_integrations.c.enabled,
            ))).mappings().one_or_none()
            return dict(row) if row is not None else None

    async def record_scm_pull_request(
        self, task_id: str | UUID, pr: dict, *, opened: bool = False,
    ) -> dict:
        async with self._transaction() as session:
            task = await self._task(session, task_id, True)
            previous = task["result"].get("pr")
            if previous and previous["state"] == "merged":
                return previous
            if pr != previous:
                await session.execute(update(tasks).where(tasks.c.id == task["id"]).values(
                    result={**task["result"], "pr": pr}, updated_at=func.now(),
                ))
            if opened:
                await self._event(session, task["id"], "pr.opened", pr, "pr.opened")
            return pr

    async def scm_check_results(self, task_id: str | UUID, turn: int) -> dict[str, dict]:
        async with self._transaction() as session:
            task = await self._task(session, task_id)
            rows = await session.execute(select(events.c.type, events.c.payload).where(
                events.c.task_id == task["id"],
                events.c.type.in_(("verification.completed", "ci.completed")),
                events.c.payload["turn"].astext == str(turn),
            ).order_by(events.c.seq))
            return dict(rows.tuples().all())
    # Champion solidification and platform default

    async def get_champion_eval(self, champion_id: UUID) -> dict:
        from sca.db.schema import champions, eval_runs, evolution_candidates

        candidate = evolution_candidates
        run = eval_runs
        async with self._transaction() as session:
            row = (await session.execute(
                select(run.c.suite_hash, run.c.variant)
                .select_from(champions.join(candidate, champions.c.candidate_id == candidate.c.id)
                             .join(run, candidate.c.dev_run_id == run.c.id))
                .where(champions.c.id == champion_id, champions.c.tenant_id == self.tenant_id,
                       candidate.c.tenant_id == self.tenant_id, run.c.tenant_id == self.tenant_id)
            )).mappings().one()
            return dict(row)

    async def create_solidify_eval_runs(self, runs: list[dict]) -> None:
        from sca.db.schema import eval_runs

        async with self._transaction() as session:
            await session.execute(pg_insert(eval_runs).values([
                {**run, "tenant_id": self.tenant_id} for run in runs
            ]).on_conflict_do_nothing())

    async def set_default_opencode_version(self, version: str) -> None:
        from sca.db.schema import platform_settings

        if self.tenant_id != "eval":
            raise ValueError("The platform default requires the eval operator tenant")
        async with self._transaction() as session:
            await session.execute(pg_insert(platform_settings).values(
                tenant_id=self.tenant_id, opencode_default_version=version,
            ).on_conflict_do_update(
                index_elements=[platform_settings.c.tenant_id],
                set_={"opencode_default_version": version},
            ))

    async def resolve_task_opencode_version(self, task_id: str, configured: str) -> str:
        from sca.db.schema import platform_settings

        async with self._transaction() as session:
            task = await self._task(session, task_id, lock=True)
            result = task["result"]
            if selected := result.get("agent_version"):
                return selected
            # A sandbox created by an older worker already mounted the configured default.
            provisioned = result.get("sandbox_id") or any(
                key.startswith("subtask.") and key.endswith(".sandbox_id") for key in result
            )
            version = configured
            if not provisioned:
                version = await session.scalar(select(
                    platform_settings.c.opencode_default_version,
                ).where(platform_settings.c.tenant_id == "eval")) or configured
            await session.execute(update(tasks).where(tasks.c.id == task["id"]).values(
                result={**result, "agent_version": version}, updated_at=func.now(),
            ))
            return version
    # MCP tool registry and task grants

    def _feature_tenant(self) -> str:
        if not self.tenant_id:
            raise ValueError("Tool and knowledge operations require a tenant")
        return self.tenant_id

    async def task_submitter(self, task_id: UUID) -> str | None:
        tenant = self._feature_tenant()
        async with self._transaction() as session:
            payload = await session.scalar(select(events.c.payload).where(
                events.c.task_id == task_id, events.c.tenant_id == tenant,
                events.c.type == "task.created",
            ))
        return payload.get("actor") if payload else None

    def _visible_tools(self):
        from sca.db.schema import tools
        from sca.tools.models import PLATFORM

        tenant = self._feature_tenant()
        return select(tools).where(tools.c.tenant_id.in_((tenant, PLATFORM)))

    async def list_tools(self) -> list[dict]:
        from sca.db.schema import tools

        async with self._transaction() as session:
            rows = await session.execute(self._visible_tools().order_by(tools.c.name, tools.c.tenant_id))
            return [dict(row) for row in rows.mappings()]

    async def get_tool(self, name: str) -> dict:
        from sca.db.schema import tools

        statement = self._visible_tools().where(tools.c.name == name).order_by(
            (tools.c.tenant_id == self.tenant_id).desc()
        )
        async with self._transaction() as session:
            row = (await session.execute(statement)).mappings().first()
            if row is None:
                raise NotFoundError("工具不存在")
            return dict(row)

    async def create_tool(self, values: dict, actor: str) -> dict:
        from sca.db.schema import tools

        statement = pg_insert(tools).values(
            **values, tenant_id=self._feature_tenant(), created_by=actor,
        ).on_conflict_do_nothing().returning(tools)
        async with self._transaction() as session:
            row = (await session.execute(statement)).mappings().first()
            if row is None:
                raise ConflictError("该工具名称已存在")
            return dict(row)

    async def patch_tool(self, name: str, changes: dict) -> dict:
        from pydantic import ValidationError

        from sca.core.errors import InvalidTaskError
        from sca.db.schema import tools
        from sca.tools.models import ToolDefinition

        owned = (tools.c.tenant_id == self._feature_tenant(), tools.c.name == name)
        async with self._transaction() as session:
            row = (await session.execute(select(tools).where(*owned).with_for_update())).mappings().first()
            if row is None:
                raise NotFoundError("工具不存在")
            if not changes:
                return dict(row)
            current = {key: row[key] for key in ToolDefinition.model_fields}
            try:
                values = ToolDefinition.model_validate({**current, **changes}).model_dump()
            except ValidationError:
                raise InvalidTaskError("工具配置无效，请检查 URL、类型和请求头配置") from None
            updated = await session.execute(update(tools).where(*owned).values(
                **values, version=tools.c.version + 1,
            ).returning(tools))
            return dict(updated.mappings().one())

    async def delete_tool(self, name: str) -> None:
        from sqlalchemy import delete

        from sca.db.schema import tools

        async with self._transaction() as session:
            removed = await session.scalar(delete(tools).where(
                tools.c.tenant_id == self._feature_tenant(), tools.c.name == name,
            ).returning(tools.c.name))
            if removed is None:
                raise NotFoundError("工具不存在")

    async def list_tool_grants(self, owner: str, name: str, tenant: str) -> list[dict]:
        from sca.db.schema import tool_grants as grants

        query = select(grants).where(grants.c.tenant_id == tenant, grants.c.tool_tenant_id == owner,
                                    grants.c.tool_name == name)
        async with self._transaction() as session:
            rows = await session.execute(query.order_by(grants.c.subject_type, grants.c.subject))
            return [dict(row) for row in rows.mappings()]

    async def add_tool_grant(self, values: dict, actor: str) -> dict:
        from sqlalchemy.exc import IntegrityError

        from sca.db.schema import tool_grants

        statement = pg_insert(tool_grants).values(**values, granted_by=actor).on_conflict_do_update(
            index_elements=list(tool_grants.primary_key.columns),
            set_={"granted_by": tool_grants.c.granted_by},
        ).returning(tool_grants)
        try:
            async with self._transaction() as session:
                return dict((await session.execute(statement)).mappings().one())
        except IntegrityError:
            raise NotFoundError("工具不存在") from None

    async def delete_tool_grant(self, owner: str, name: str, tenant: str,
                                subject_type: str, subject: str) -> None:
        from sqlalchemy import delete

        from sca.db.schema import tool_grants as grants

        async with self._transaction() as session:
            await session.execute(delete(grants).where(
                grants.c.tenant_id == tenant, grants.c.tool_tenant_id == owner,
                grants.c.tool_name == name, grants.c.subject_type == subject_type,
                grants.c.subject == subject,
            ))

    async def available_tools(self, actor: str, teams: list[str], *,
                              names: list[str] | None = None) -> list[dict]:
        from sqlalchemy import and_, or_

        from sca.db.schema import tool_grants as grants
        from sca.db.schema import tools

        tenant = self._feature_tenant()
        visible = self._visible_tools()
        if names is not None:
            visible = visible.where(tools.c.name.in_(names))
        chosen = visible.distinct(tools.c.name).order_by(
            tools.c.name, (tools.c.tenant_id == tenant).desc(),
        ).subquery()
        granted = select(grants.c.subject).where(
            grants.c.tenant_id == tenant, grants.c.tool_tenant_id == chosen.c.tenant_id,
            grants.c.tool_name == chosen.c.name,
            or_(and_(grants.c.subject_type == "tenant", grants.c.subject == tenant),
                and_(grants.c.subject_type == "user", grants.c.subject == actor),
                and_(grants.c.subject_type == "team", grants.c.subject.in_(teams))),
        ).exists()
        async with self._transaction() as session:
            rows = await session.execute(select(chosen).where(chosen.c.enabled, granted).order_by(chosen.c.name))
            return [dict(row) for row in rows.mappings()]

    # Tenant knowledge base

    async def _has_knowledge_vectors(self, session: AsyncSession) -> bool:
        return bool(await session.scalar(text("""SELECT EXISTS (
            SELECT 1 FROM pg_attribute WHERE attrelid = 'kb_chunks'::regclass
            AND attname = 'embedding' AND NOT attisdropped
        )""")))

    async def create_knowledge_document(self, document: dict, chunks: list[dict],
                                        embedding_model: str | None) -> dict:
        from sca.db.schema import kb_chunks, kb_documents
        from sca.memory.embedding import literal

        tenant, doc_id = self._feature_tenant(), uuid4()
        async with self._transaction() as session:
            row = (await session.execute(insert(kb_documents).values(
                **document, id=doc_id, tenant_id=tenant, chunk_count=len(chunks),
            ).returning(kb_documents))).mappings().one()
            await session.execute(insert(kb_chunks), [
                {"tenant_id": tenant, "document_id": doc_id, "ordinal": chunk["ordinal"],
                 "heading": chunk["heading"], "content": chunk["content"]}
                for chunk in chunks
            ])
            if embedding_model and await self._has_knowledge_vectors(session):
                vectors = [{"tenant": tenant, "document": doc_id, "ordinal": chunk["ordinal"],
                            "embedding": literal(chunk["embedding"]), "model": embedding_model}
                           for chunk in chunks if chunk["embedding"] is not None]
                if vectors:
                    await session.execute(text("""UPDATE kb_chunks SET
                        embedding = CAST(:embedding AS vector), embedding_model = :model
                        WHERE tenant_id = :tenant AND document_id = :document AND ordinal = :ordinal
                    """), vectors)
            return dict(row)

    def _readable_knowledge(self, teams: list[str]):
        from sqlalchemy import or_

        from sca.db.schema import kb_documents as docs

        return (docs.c.tenant_id == self._feature_tenant(),
                or_(func.cardinality(docs.c.allowed_teams) == 0, docs.c.allowed_teams.overlap(teams)))

    async def list_knowledge_documents(self, teams: list[str], limit: int, offset: int) -> list[dict]:
        from sca.db.schema import kb_documents as docs

        metadata = [column for column in docs.c if column.key != "content"]
        query = select(*metadata).where(*self._readable_knowledge(teams)).order_by(
            docs.c.created_at.desc(), docs.c.id,
        ).limit(limit).offset(offset)
        async with self._transaction() as session:
            return [dict(row) for row in (await session.execute(query)).mappings()]

    async def get_knowledge_document(self, document_id: UUID, teams: list[str]) -> dict:
        from sca.db.schema import kb_chunks as chunks
        from sca.db.schema import kb_documents as docs

        async with self._transaction() as session:
            row = (await session.execute(select(docs).where(
                docs.c.id == document_id, *self._readable_knowledge(teams),
            ).with_for_update(read=True, key_share=True))).mappings().first()
            if row is None:
                raise NotFoundError("知识库文档不存在")
            citations = await session.execute(select(chunks.c.ordinal, chunks.c.heading).where(
                chunks.c.tenant_id == self.tenant_id, chunks.c.document_id == document_id,
            ).order_by(chunks.c.ordinal))
            return {**row, "citations": [dict(citation) for citation in citations.mappings()]}

    async def delete_knowledge_document(self, document_id: UUID) -> None:
        from sqlalchemy import delete

        from sca.db.schema import kb_documents as docs

        async with self._transaction() as session:
            removed = await session.scalar(delete(docs).where(
                docs.c.tenant_id == self._feature_tenant(), docs.c.id == document_id,
            ).returning(docs.c.id))
            if removed is None:
                raise NotFoundError("知识库文档不存在")

    async def search_knowledge(self, query: str, teams: list[str], limit: int,
                               vector: list[float] | None, model: str | None) -> list[dict]:
        from sqlalchemy import and_, case, literal_column, null, or_

        from sca.db.schema import kb_chunks as chunks
        from sca.db.schema import kb_documents as docs
        from sca.memory.embedding import literal

        parameters = {}
        async with self._transaction() as session:
            distance = null()
            if vector and model and await self._has_knowledge_vectors(session):
                embedding = literal_column("kb_chunks.embedding")
                distance = case((
                    and_(chunks.c.embedding_model == model, func.vector_dims(embedding) == len(vector)),
                    embedding.op("<=>")(text("CAST(:query_vector AS vector)")),
                ))
                parameters["query_vector"] = literal(vector)
            candidates = select(
                chunks.c.document_id, docs.c.title, chunks.c.ordinal, chunks.c.heading,
                chunks.c.content, distance.label("distance"),
            ).select_from(chunks.join(docs)).where(*self._readable_knowledge(teams)).subquery()
            matches = or_(candidates.c.distance.is_not(None), *[
                column.icontains(query, autoescape=True)
                for column in (candidates.c.title, candidates.c.heading, candidates.c.content)
            ])
            result = await session.execute(select(candidates).where(matches).order_by(
                candidates.c.distance.asc().nulls_last(), candidates.c.document_id, candidates.c.ordinal,
            ).limit(limit), parameters)
            return [dict(row) for row in result.mappings()]
