"""Run slots and tenant scheduling weights."""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN weight integer NOT NULL DEFAULT 1 CHECK (weight > 0)")
    op.execute("ALTER TABLE tasks ADD COLUMN slot_held boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE tasks ADD COLUMN slot_requested_at timestamptz DEFAULT now()")
    op.execute("ALTER TABLE tasks ADD COLUMN slot_generation integer NOT NULL DEFAULT 0")
    op.execute("UPDATE tasks SET slot_requested_at = NULL WHERE state <> 'QUEUED'")
    op.execute("UPDATE tasks SET slot_held = true WHERE state IN ('PROVISIONING','RUNNING','VERIFYING')")
    op.execute("CREATE INDEX ix_tasks_admission ON tasks (slot_requested_at) WHERE slot_requested_at IS NOT NULL OR slot_held")


def downgrade() -> None:
    op.execute("DROP INDEX ix_tasks_admission")
    for column in ("slot_generation", "slot_requested_at", "slot_held"):
        op.drop_column("tasks", column)
    op.drop_column("tenants", "weight")
