import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def timestamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("spec", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("state_reason", sa.Text()),
        sa.Column(
            "result", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "usage", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("idempotency_key", sa.Text()),
        timestamp("created_at"),
        timestamp("updated_at"),
        sa.UniqueConstraint("tenant_id", "idempotency_key"),
    )
    op.create_index("ix_tasks_tenant_created", "tasks", ["tenant_id", sa.text("created_at DESC")])
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("task_id", sa.UUID(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("dedup_key", sa.Text()),
        timestamp("created_at"),
        sa.UniqueConstraint("task_id", "seq"),
        sa.UniqueConstraint("task_id", "dedup_key"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("task_id", sa.UUID(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("backend_session_id", sa.Text()),
        timestamp("created_at"),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("task_id", sa.UUID(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        timestamp("created_at"),
        sa.UniqueConstraint("task_id", "name"),
    )


def downgrade() -> None:
    for name in ("artifacts", "sessions", "events", "tasks"):
        op.drop_table(name)
