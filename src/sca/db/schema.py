from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
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
