"""Let the API read evaluation runs under tenant isolation (the Portal lists them)."""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

TABLES = ("eval_runs", "eval_results")


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""")
        op.execute(f"GRANT SELECT ON {table} TO sca_api")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"REVOKE SELECT ON {table} FROM sca_api")
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
