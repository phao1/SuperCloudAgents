from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE memories (
        id uuid PRIMARY KEY, tenant_id text NOT NULL,
        scope text NOT NULL CHECK (scope = 'tenant' OR scope ~ '^(repo|kind):.+'),
        text text NOT NULL CHECK (length(btrim(text)) BETWEEN 1 AND 500),
        category text NOT NULL CHECK (category IN ('convention','pitfall','command','preference')),
        status text NOT NULL CHECK (status IN ('proposed','active','rejected','retired')),
        source_task_id uuid REFERENCES tasks(id),
        source text NOT NULL CHECK (source IN ('manual','reviewer_feedback','agent_proposal')),
        created_by text NOT NULL, approved_by text,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(), approved_at timestamptz,
        uses bigint NOT NULL DEFAULT 0, last_used_at timestamptz
    )""")
    op.execute("CREATE INDEX ix_memories_scope ON memories (tenant_id, scope, status)")
    op.execute("""CREATE TABLE memory_events (
        id uuid PRIMARY KEY, tenant_id text NOT NULL, memory_id uuid NOT NULL,
        actor text NOT NULL, action text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    )""")
    for table in ("memories", "memory_events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON memories TO sca_api")
    op.execute("GRANT SELECT, INSERT ON memory_events TO sca_api")


def downgrade() -> None:
    op.drop_table("memory_events")
    op.drop_table("memories")
