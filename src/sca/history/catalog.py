"""The history format's column allowlist and snapshot queries."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, Table, and_, cast, func, or_, select, union
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.selectable import CTE, FromClause
from sqlalchemy.types import Text

from sca.db import schema as db


@dataclass(frozen=True)
class HistoryTable:
    table: Table
    columns: str
    # (foreign key column, parent, require an imported parent before attaching children)
    parents: tuple[tuple[str, Table, bool], ...] = ()

    @property
    def name(self) -> str:
        return self.table.name

    @property
    def member(self) -> str:
        return f"tables/{self.name}.jsonl"


TENANTS = HistoryTable(
    db.tenants, "id display_name created_at status deleted_at membership_required",
)
PROJECTS = HistoryTable(
    db.projects, "id tenant_id name goal instructions default_backend default_model repos settings "
    "status is_default created_by created_at updated_at deleted_at",
)
TASKS = HistoryTable(
    db.tasks, "id tenant_id project_id kind spec state state_reason result usage "
    "created_at updated_at finished_at events_archived_at",
)
ARTIFACTS = HistoryTable(
    db.artifacts, "id tenant_id task_id name content_type size_bytes created_at",
    (("task_id", db.tasks, True),),
)
EVAL_RUNS = HistoryTable(
    db.eval_runs, "id tenant_id suite suite_hash variant created_by status created_at finished_at",
)
EVOLUTIONS = HistoryTable(
    db.evolutions, "id tenant_id status config created_by created_at updated_at finished_at "
    "best_champion_id spent_usd",
)
CANDIDATES = HistoryTable(
    db.evolution_candidates, "id tenant_id evolution_id generation parent_id overlay "
    "overlay_sha256 hypothesis diagnosis static_check dev_run_id holdout_run_id dev_metrics "
    "holdout_metrics decision reason created_at",
    (("evolution_id", db.evolutions, True),),
)
CHAMPIONS = HistoryTable(
    db.champions, "id tenant_id agent_version model overlay overlay_sha256 candidate_id "
    "metrics parent_champion_id created_at active",
    (("candidate_id", db.evolution_candidates, False),),
)
EVOLUTION_EVENTS = HistoryTable(
    db.evolution_events, "id tenant_id type actor payload created_at",
)
TABLES = (
    TENANTS, PROJECTS, TASKS,
    HistoryTable(
        db.sessions, "id tenant_id task_id backend backend_session_id created_at",
        (("task_id", db.tasks, True),),
    ),
    HistoryTable(
        db.events, "tenant_id task_id seq type payload created_at",
        (("task_id", db.tasks, True),),
    ),
    HistoryTable(
        db.task_messages, "id tenant_id task_id seq text mode status actor created_at "
        "turn plan_attempt", (("task_id", db.tasks, True),),
    ),
    ARTIFACTS, EVAL_RUNS,
    HistoryTable(
        db.eval_results, "run_id tenant_id case_id repeat task_id acceptance_passed "
        "trajectory_passed trajectory_findings verification_passed turns tokens cost_usd duration_s",
        (("run_id", db.eval_runs, True), ("task_id", db.tasks, False)),
    ),
    EVOLUTIONS, CANDIDATES, CHAMPIONS, EVOLUTION_EVENTS,
)


def _ids(table: FromClause, *conditions: ColumnElement[bool] | bool) -> Select:
    return select(table.c.id, table.c.tenant_id).where(*conditions)


def _same_tenant(left: FromClause, right: FromClause, key: str = "id") -> ColumnElement[bool]:
    return and_(left.c[key] == right.c.id, left.c.tenant_id == right.c.tenant_id)


class Snapshot:
    def __init__(self, tenants: tuple[str, ...], since: datetime | None) -> None:
        self.tenants = tenants
        self.since = since
        self.owners: dict[Table, CTE] = {
            table: _ids(table, self._tenant(table.c.tenant_id)).cte()
            for table in (db.tasks, db.eval_runs, db.evolutions, db.champions)
        }
        if since is not None:
            self._recent(since)

    def _tenant(self, column: ColumnElement[str]) -> ColumnElement[bool] | bool:
        return column.in_(self.tenants) if self.tenants else True

    def _recent(self, since: datetime) -> None:
        tasks, runs, evolutions, champions, candidates = (
            db.tasks, db.eval_runs, db.evolutions, db.champions, db.evolution_candidates,
        )
        recent_runs = select(runs).where(
            self._tenant(runs.c.tenant_id),
            func.greatest(runs.c.created_at, runs.c.finished_at) >= since,
        ).cte()
        recent_evolutions = _ids(
            evolutions, self._tenant(evolutions.c.tenant_id),
            or_(
                func.greatest(evolutions.c.updated_at, evolutions.c.finished_at) >= since,
                select(1).where(
                    recent_runs.c.tenant_id == evolutions.c.tenant_id,
                    recent_runs.c.variant["evolution_id"].astext == cast(evolutions.c.id, Text),
                ).exists(),
            ),
        ).cte()
        champion_ids = _ids(
            champions, self._tenant(champions.c.tenant_id),
            or_(
                champions.c.created_at >= since,
                select(1).select_from(candidates.join(
                    recent_evolutions, _same_tenant(candidates, recent_evolutions, "evolution_id"),
                )).where(_same_tenant(champions, candidates, "candidate_id")).exists(),
                select(1).select_from(evolutions.join(
                    recent_evolutions, _same_tenant(evolutions, recent_evolutions),
                )).where(_same_tenant(evolutions, champions, "best_champion_id")).exists(),
            ),
        ).cte("history_champions", recursive=True)
        self.owners[champions] = champion_ids.union(self._champion_parents(champion_ids))
        selected_champions = self.owners[champions]
        self.owners[evolutions] = recent_evolutions.select().union(
            select(candidates.c.evolution_id, candidates.c.tenant_id)
            .select_from(champions.join(selected_champions, _same_tenant(champions, selected_champions))
                         .join(candidates, _same_tenant(champions, candidates, "candidate_id")))
        ).cte()

        evolution_ids = self.owners[evolutions]
        linked_runs = select(1).select_from(candidates.join(
            evolution_ids, _same_tenant(candidates, evolution_ids, "evolution_id"),
        )).where(
            candidates.c.tenant_id == runs.c.tenant_id,
            or_(candidates.c.dev_run_id == runs.c.id, candidates.c.holdout_run_id == runs.c.id),
        ).exists()
        run_ids = self.owners[runs] = _ids(
            runs, self._tenant(runs.c.tenant_id),
            or_(
                runs.c.id.in_(select(recent_runs.c.id)), linked_runs,
                select(1).where(
                    evolution_ids.c.tenant_id == runs.c.tenant_id,
                    runs.c.variant["evolution_id"].astext == cast(evolution_ids.c.id, Text),
                ).exists(),
                select(1).where(
                    tasks.c.tenant_id == runs.c.tenant_id, tasks.c.updated_at >= since,
                    tasks.c.spec["eval_run_id"].astext == cast(runs.c.id, Text),
                ).exists(),
            ),
        ).cte()

        results = db.eval_results
        seeds = _ids(
            tasks, self._tenant(tasks.c.tenant_id),
            or_(
                tasks.c.updated_at >= since,
                select(1).select_from(results.join(run_ids, _same_tenant(results, run_ids, "run_id")))
                .where(_same_tenant(results, tasks, "task_id")).exists(),
                select(1).where(
                    tasks.c.tenant_id == run_ids.c.tenant_id,
                    tasks.c.spec["eval_run_id"].astext == cast(run_ids.c.id, Text),
                ).exists(),
            ),
        )
        self.owners[tasks] = self._task_graph(seeds)

    @staticmethod
    def _champion_parents(selected: CTE) -> Select:
        child, parent = db.champions.alias("child"), db.champions
        return _ids(parent).select_from(
            child.join(selected, _same_tenant(child, selected))
            .join(parent, _same_tenant(child, parent, "parent_champion_id"))
        )

    def _task_graph(self, seeds: Select) -> CTE:
        tasks, events = db.tasks, db.events
        parent = tasks.c.result["parent_task_id"].astext
        child = func.coalesce(
            events.c.payload["child_task_id"].astext, events.c.payload["task_id"].astext,
        )
        edges = union(
            select(cast(tasks.c.id, Text).label("child"), parent.label("parent"), tasks.c.tenant_id)
            .where(self._tenant(tasks.c.tenant_id), parent.is_not(None)),
            select(child, cast(events.c.task_id, Text), events.c.tenant_id).where(
                self._tenant(events.c.tenant_id), child.is_not(None),
                events.c.type.in_(("task.child_started", "subtask.started")),
            ),
            select(func.jsonb_array_elements_text(tasks.c.result["child_task_ids"]),
                   cast(tasks.c.id, Text), tasks.c.tenant_id)
            .where(self._tenant(tasks.c.tenant_id),
                   func.jsonb_typeof(tasks.c.result["child_task_ids"]) == "array"),
            select(func.jsonb_extract_path_text(
                       func.jsonb_array_elements(tasks.c.result["subtasks"]), "task_id",
                   ),
                   cast(tasks.c.id, Text), tasks.c.tenant_id)
            .where(self._tenant(tasks.c.tenant_id),
                   func.jsonb_typeof(tasks.c.result["subtasks"]) == "array"),
        ).cte("history_edges")
        # Traverse both ways so a recent child and an older parent both keep their graph.
        links = select(edges).union(
            select(edges.c.parent, edges.c.child, edges.c.tenant_id),
        ).cte("history_links")
        selected = seeds.cte("history_tasks", recursive=True)
        return selected.union(_ids(tasks).select_from(
            selected.join(links, and_(
                links.c.tenant_id == selected.c.tenant_id,
                links.c.parent == cast(selected.c.id, Text),
            )).join(tasks, and_(
                tasks.c.tenant_id == links.c.tenant_id, cast(tasks.c.id, Text) == links.c.child,
            ))
        ))

    def _tenant_rows(self) -> Select:
        t = db.tenants.c
        # Some older eval runs have a tenant key without a corresponding tenants row.
        ids = union(
            select(t.id).where(self._tenant(t.id)),
            *(select(table.c.tenant_id).where(self._tenant(table.c.tenant_id))
              for table in (*self.owners, db.evolution_events)),
        ).cte("history_tenants")
        return select(
            ids.c.id, func.coalesce(t.display_name, ids.c.id).label("display_name"),
            func.coalesce(t.created_at, func.now()).label("created_at"),
            func.coalesce(t.status, "active").label("status"), t.deleted_at,
            func.coalesce(t.membership_required, True).label("membership_required"),
        ).select_from(ids.outerjoin(db.tenants, t.id == ids.c.id)).order_by(ids.c.id)

    def rows(self, entry: HistoryTable) -> Select:
        table = entry.table
        if entry is TENANTS:
            return self._tenant_rows()
        stmt = select(*(table.c[name] for name in entry.columns.split()))
        if table in self.owners:
            owner = self.owners[table]
            stmt = stmt.join(owner, _same_tenant(table, owner))
        for key, parent, _ in entry.parents:
            if parent in self.owners:
                owner = self.owners[parent]
                stmt = stmt.join(owner, _same_tenant(table, owner, key))
            elif parent is db.evolution_candidates:
                stmt = stmt.outerjoin(parent, _same_tenant(table, parent, key)).where(
                    or_(table.c[key].is_(None), parent.c.id.is_not(None)),
                )
        if entry is ARTIFACTS:
            stmt = stmt.join(db.tasks, _same_tenant(table, db.tasks, "task_id")).add_columns(
                db.tasks.c.result["history_imported"].label("imported_task"),
            )
        if self.since is not None and entry is PROJECTS:
            selected = self.owners[db.tasks]
            stmt = stmt.where(or_(
                table.c.updated_at >= self.since,
                select(1).select_from(db.tasks.join(selected, _same_tenant(db.tasks, selected)))
                .where(_same_tenant(db.tasks, table, "project_id")).exists(),
            ))
        if self.since is not None and entry is EVOLUTION_EVENTS:
            stmt = stmt.where(table.c.created_at >= self.since)
        return stmt.where(self._tenant(table.c.tenant_id)).order_by(*table.primary_key)
