from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
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
    Column("weight", Integer, nullable=False, server_default="1"),
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
    Column("slot_held", Boolean, nullable=False, server_default="false"),
    Column("slot_requested_at", DateTime(timezone=True), server_default=func.now()),
    Column("slot_generation", Integer, nullable=False, server_default="0"),
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

# Evolution candidates carry their own tenant key so RLS applies to direct reads too.
evolutions = Table(
    'evolutions', metadata,
    Column('id', UUID(as_uuid=True), primary_key=True),
    Column('tenant_id', Text, nullable=False),
    Column('status', Text, nullable=False),
    Column('config', JSONB, nullable=False),
    Column('created_by', Text, nullable=False),
    timestamp('created_at'), timestamp('updated_at'),
    Column('finished_at', DateTime(timezone=True)),
    Column('best_champion_id', UUID(as_uuid=True)),
    Column('spent_usd', Numeric, nullable=False, server_default='0'),
)
evolution_candidates = Table(
    'evolution_candidates', metadata,
    Column('id', UUID(as_uuid=True), primary_key=True),
    Column('tenant_id', Text, nullable=False),
    Column('evolution_id', UUID(as_uuid=True), ForeignKey('evolutions.id'), nullable=False),
    Column('generation', Integer, nullable=False),
    Column('parent_id', UUID(as_uuid=True)),
    Column('overlay', JSONB, nullable=False),
    Column('overlay_sha256', Text, nullable=False),
    Column('hypothesis', Text, nullable=False),
    Column('diagnosis', JSONB, nullable=False),
    Column('static_check', JSONB, nullable=False),
    Column('dev_run_id', UUID(as_uuid=True)),
    Column('holdout_run_id', UUID(as_uuid=True)),
    Column('dev_metrics', JSONB), Column('holdout_metrics', JSONB),
    Column('decision', Text, nullable=False), Column('reason', Text),
    timestamp('created_at'),
    CheckConstraint("decision IN ('pending','rejected','promoted','invalid')"),
)
champions = Table(
    'champions', metadata,
    Column('id', UUID(as_uuid=True), primary_key=True),
    Column('tenant_id', Text, nullable=False),
    Column('agent_version', Text, nullable=False),
    Column('model', Text, nullable=False),
    Column('overlay', JSONB, nullable=False),
    Column('overlay_sha256', Text, nullable=False),
    Column('candidate_id', UUID(as_uuid=True), ForeignKey('evolution_candidates.id')),
    Column('metrics', JSONB, nullable=False),
    Column('parent_champion_id', UUID(as_uuid=True)),
    timestamp('created_at'),
    Column('active', Boolean, nullable=False, server_default='false'),
)
Index('ix_champions_active', champions.c.tenant_id, champions.c.agent_version, champions.c.model,
      unique=True, postgresql_where=champions.c.active)

# Evolution productization
champion_rollouts = Table(
    "champion_rollouts", metadata,
    Column("tenant_id", Text, primary_key=True),
    Column("agent_version", Text, primary_key=True),
    Column("model", Text, primary_key=True),
    Column("champion_id", UUID(as_uuid=True), ForeignKey("champions.id"), nullable=False),
    Column("percent", Integer, nullable=False),
    Column("updated_by", Text, nullable=False),
    timestamp("updated_at"),
    CheckConstraint("percent BETWEEN 0 AND 100"),
)

evolution_events = Table(
    "evolution_events", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("actor", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    timestamp("created_at"),
)

evolution_schedules = Table(
    "evolution_schedules", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("suite", Text, nullable=False),
    Column("split", Text, nullable=False, server_default="dev"),
    Column("agent_version", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("interval_hours", Float, nullable=False),
    Column("config", JSONB, nullable=False),
    Column("threshold", Float, nullable=False),
    Column("always", Boolean, nullable=False, server_default="false"),
    Column("enabled", Boolean, nullable=False, server_default="true"),
    Column("auto_rollout_percent", Integer),
    Column("last_run_at", DateTime(timezone=True)),
    Column("last_eval_run_id", UUID(as_uuid=True), ForeignKey("eval_runs.id")),
    Column("last_evolution_id", UUID(as_uuid=True), ForeignKey("evolutions.id")),
    CheckConstraint("split = 'dev'"),
    CheckConstraint("interval_hours > 0 AND interval_hours < 'Infinity'::float8"),
    CheckConstraint("threshold BETWEEN 0 AND 1"),
    CheckConstraint("auto_rollout_percent BETWEEN 0 AND 100"),
)

harvest_candidates = Table(
    "harvest_candidates", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("task_id", UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False),
    Column("suite", Text, nullable=False),
    Column("split", Text, nullable=False),
    Column("case_id", Text, nullable=False),
    Column("base_sha", Text, nullable=False),
    Column("notes", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("created_by", Text, nullable=False),
    timestamp("created_at"),
    Column("reviewed_by", Text),
    Column("reviewed_at", DateTime(timezone=True)),
    CheckConstraint("split IN ('dev', 'holdout')"),
    CheckConstraint("status IN ('pending', 'approved', 'rejected')"),
)
# Tenant operations and billing
plans = Table(
    "plans", metadata,
    Column("name", Text, primary_key=True),
    Column("monthly_cost_limit_usd", Numeric),
    Column("max_concurrent_tasks", Integer),
    Column("max_task_cost_usd", Numeric),
    Column("features", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    CheckConstraint("monthly_cost_limit_usd >= 0 AND monthly_cost_limit_usd < 'Infinity'::numeric"),
    CheckConstraint("max_concurrent_tasks > 0"),
    CheckConstraint("max_task_cost_usd >= 0 AND max_task_cost_usd < 'Infinity'::numeric"),
)

tenants.append_column(Column("display_name", Text, nullable=False))
tenants.append_column(Column("plan", Text, ForeignKey("plans.name"), nullable=False,
                             server_default="enterprise"))
tenants.append_column(Column("status", Text, nullable=False, server_default="active"))
tenants.append_column(Column("membership_required", Boolean, nullable=False, server_default="false"))
tenants.append_column(Column("deleted_at", DateTime(timezone=True)))
tenants.append_constraint(CheckConstraint("status IN ('active', 'suspended')", name="tenants_status"))

tenant_members = Table(
    "tenant_members", metadata,
    Column("tenant_id", Text, ForeignKey("tenants.id"), primary_key=True),
    Column("email", Text, primary_key=True),
    Column("subject", Text),
    Column("role", Text, nullable=False),
    timestamp("created_at"),
    Column("activated_at", DateTime(timezone=True)),
    UniqueConstraint("tenant_id", "subject"),
    CheckConstraint("role IN ('owner', 'admin', 'member', 'viewer')"),
)

usage_daily = Table(
    "usage_daily", metadata,
    Column("tenant_id", Text, ForeignKey("tenants.id"), primary_key=True),
    Column("day", Date, primary_key=True),
    Column("tasks", BigInteger, nullable=False, server_default="0"),
    Column("turns", BigInteger, nullable=False, server_default="0"),
    Column("tokens_in", BigInteger, nullable=False, server_default="0"),
    Column("tokens_out", BigInteger, nullable=False, server_default="0"),
    Column("cost_usd", Numeric, nullable=False, server_default="0"),
    Column("sandbox_seconds", BigInteger, nullable=False, server_default="0"),
    Column("by_model", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
)

tasks.append_column(Column("finished_at", DateTime(timezone=True)))
tasks.append_column(Column("billing_usage", JSONB))
# SCM
scm_integrations = Table(
    "scm_integrations", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("host", Text, nullable=False),
    Column("repo_full_name", Text, nullable=False),
    Column("webhook_secret_ref", Text, nullable=False),
    Column("default_spec", JSONB, nullable=False),
    Column("enabled", Boolean, nullable=False, server_default="true"),
    timestamp("created_at"), timestamp("updated_at"),
    CheckConstraint("provider IN ('github', 'gitea')"),
    UniqueConstraint("tenant_id", "provider", "host", "repo_full_name"),
    UniqueConstraint("tenant_id", "webhook_secret_ref"),
)
