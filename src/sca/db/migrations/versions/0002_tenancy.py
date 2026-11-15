import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("max_active_tasks", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute(
        "INSERT INTO tenants (id, max_active_tasks) SELECT DISTINCT tenant_id, 20 FROM tasks"
    )
    for table in ("events", "sessions", "artifacts"):
        op.add_column(table, sa.Column("tenant_id", sa.Text()))
        op.execute(
            f"UPDATE {table} SET tenant_id = tasks.tenant_id "
            f"FROM tasks WHERE {table}.task_id = tasks.id"
        )
        op.alter_column(table, "tenant_id", nullable=False)
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sca_api') THEN
            CREATE ROLE sca_api NOLOGIN;
        END IF;
    END $$""")
    for table in ("tasks", "events", "sessions", "artifacts"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (tenant_id = current_setting('app.tenant_id', true)) "
            "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO sca_api")
    op.execute("GRANT SELECT, INSERT, UPDATE ON tenants TO sca_api")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE events_id_seq TO sca_api")


def downgrade() -> None:
    for table in ("tasks", "events", "sessions", "artifacts"):
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    for table in ("events", "sessions", "artifacts"):
        op.drop_column(table, "tenant_id")
    op.drop_table("tenants")
