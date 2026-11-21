"""Persist guidance sent to an in-progress task."""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE task_messages (
        id uuid PRIMARY KEY,
        task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        tenant_id text NOT NULL,
        seq bigint NOT NULL,
        text text NOT NULL CHECK (char_length(text) BETWEEN 1 AND 4000),
        mode text NOT NULL CHECK (mode IN ('queue', 'interrupt')),
        status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'applied')),
        actor text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        turn integer,
        plan_attempt integer,
        UNIQUE (task_id, seq)
    )""")
    op.execute("ALTER TABLE task_messages ENABLE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_isolation ON task_messages
        USING (tenant_id = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)
            AND EXISTS (SELECT 1 FROM tasks
                        WHERE tasks.id = task_messages.task_id
                          AND tasks.tenant_id = task_messages.tenant_id))""")
    op.execute("""GRANT SELECT, INSERT (id, task_id, tenant_id, seq, text, mode, actor, created_at)
        ON task_messages TO sca_api""")


def downgrade() -> None:
    op.drop_table("task_messages")
