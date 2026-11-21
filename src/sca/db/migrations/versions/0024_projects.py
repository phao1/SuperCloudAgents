"""Tenant projects, task threads and scoped context."""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

SCOPED_TABLES = ("tasks", "kb_documents", "memories")


def upgrade() -> None:
    op.execute("""CREATE TABLE projects (
        id uuid PRIMARY KEY,
        tenant_id text NOT NULL REFERENCES tenants(id),
        name text NOT NULL,
        goal text,
        instructions text NOT NULL DEFAULT '' CHECK (char_length(instructions) <= 16000),
        default_backend text,
        default_model text,
        repos jsonb NOT NULL DEFAULT '[]'::jsonb,
        settings jsonb NOT NULL DEFAULT '{}'::jsonb,
        status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'archived')),
        is_default boolean NOT NULL DEFAULT false,
        created_by text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        deleted_at timestamptz,
        UNIQUE (tenant_id, id)
    )""")
    op.execute("""CREATE UNIQUE INDEX ix_projects_default ON projects (tenant_id)
        WHERE is_default AND deleted_at IS NULL""")
    op.execute("ALTER TABLE projects ENABLE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_isolation ON projects
        USING (tenant_id = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""")
    op.execute("GRANT SELECT, INSERT, UPDATE ON projects TO sca_api")
    for table in SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN project_id uuid")
        op.create_foreign_key(f"fk_{table}_project", table, "projects",
                              ["tenant_id", "project_id"], ["tenant_id", "id"])
        op.create_index(f"ix_{table}_project_id", table, ["project_id"])

    op.execute("""INSERT INTO tenants (id, display_name, max_active_tasks)
        SELECT DISTINCT tenant_id, tenant_id, 20 FROM tasks ON CONFLICT (id) DO NOTHING""")
    op.execute("""INSERT INTO projects (id, tenant_id, name, is_default, created_by)
        SELECT gen_random_uuid(), tenant_id, '默认项目', true, 'system:migration'
        FROM (SELECT DISTINCT tenant_id FROM tasks) AS task_tenants""")
    # Subtasks and Best-of-N attempts share their parent task row.
    op.execute("""UPDATE tasks SET project_id = projects.id FROM projects
        WHERE tasks.tenant_id = projects.tenant_id AND projects.is_default""")


def downgrade() -> None:
    for table in reversed(SCOPED_TABLES):
        op.drop_index(f"ix_{table}_project_id", table_name=table)
        op.drop_constraint(f"fk_{table}_project", table, type_="foreignkey")
        op.drop_column(table, "project_id")
    op.drop_table("projects")
