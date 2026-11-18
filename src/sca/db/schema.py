from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()


def timestamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


tenants = Table(
    "tenants",
    metadata,
    Column("id", Text, primary_key=True),
    Column("max_active_tasks", Integer, nullable=False),
    Column("daily_token_budget", BigInteger),
    Column("daily_cost_budget_usd", Numeric),
    timestamp("created_at"),
)

tasks = Table(
    "tasks",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("spec", JSONB, nullable=False),
    Column("state", Text, nullable=False),
    Column("state_reason", Text),
    Column("events_archived_at", DateTime(timezone=True)),
    Column("result", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("usage", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("idempotency_key", Text),
    timestamp("created_at"),
    timestamp("updated_at"),
    UniqueConstraint("tenant_id", "idempotency_key"),
)
Index("ix_tasks_tenant_created", tasks.c.tenant_id, tasks.c.created_at.desc())
events = Table(
    "events",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("task_id", UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("type", Text, nullable=False),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("dedup_key", Text),
    timestamp("created_at"),
    UniqueConstraint("task_id", "seq"),
    UniqueConstraint("task_id", "dedup_key"),
)
sessions = Table(
    "sessions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("task_id", UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False),
    Column("backend", Text, nullable=False),
    Column("backend_session_id", Text),
    timestamp("created_at"),
)
artifacts = Table(
    "artifacts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("task_id", UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False),
    Column("name", Text, nullable=False),
    Column("content_type", Text, nullable=False),
    Column("uri", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    timestamp("created_at"),
    UniqueConstraint("task_id", "name"),
)

api_tokens = Table(
    "api_tokens",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("token_hash", Text, nullable=False, unique=True),
    Column("role", Text),
    Column("scopes", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("repo_url", Text),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("created_by", Text, nullable=False),
    timestamp("created_at"),
    Column("revoked_at", DateTime(timezone=True)),
)

files = Table(
    "files", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("sha256", Text, nullable=False),
    timestamp("created_at"),
)

memories = Table(
    "memories", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("scope", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("category", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("source_task_id", UUID(as_uuid=True)),
    Column("source", Text, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("approved_by", Text),
    timestamp("created_at"), timestamp("updated_at"),
    Column("approved_at", DateTime(timezone=True)),
    Column("uses", BigInteger, nullable=False, server_default=text("0")),
    Column("last_used_at", DateTime(timezone=True)),
)
memory_events = Table(
    "memory_events", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("memory_id", UUID(as_uuid=True), nullable=False),
    Column("actor", Text, nullable=False),
    Column("action", Text, nullable=False),
    timestamp("created_at"),
)

eval_runs = Table(
    "eval_runs", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("suite", Text, nullable=False),
    Column("suite_hash", Text, nullable=False),
    Column("variant", JSONB, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("status", Text, nullable=False),
    timestamp("created_at"),
    Column("finished_at", DateTime(timezone=True)),
)

eval_results = Table(
    "eval_results", metadata,
    Column("run_id", UUID(as_uuid=True), ForeignKey("eval_runs.id"), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("case_id", Text, primary_key=True),
    Column("repeat", Integer, primary_key=True),
    Column("task_id", UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False),
    Column("acceptance_passed", Boolean),
    Column("trajectory_passed", Boolean),
    Column("trajectory_findings", JSONB, nullable=False, server_default="[]"),
    Column("verification_passed", Boolean),
    Column("turns", Integer, nullable=False),
    Column("tokens", BigInteger, nullable=False),
    Column("cost_usd", Numeric, nullable=False),
    Column("duration_s", Float, nullable=False),
)
