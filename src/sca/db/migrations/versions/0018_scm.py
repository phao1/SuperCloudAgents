"""Tenant-scoped source control integrations."""

from alembic import op

revision = '0018'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE scm_integrations (
        id uuid PRIMARY KEY, tenant_id text NOT NULL,
        provider text NOT NULL CHECK (provider IN ('github', 'gitea')),
        host text NOT NULL, repo_full_name text NOT NULL, webhook_secret_ref text NOT NULL,
        default_spec jsonb NOT NULL, enabled boolean NOT NULL DEFAULT true,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, provider, host, repo_full_name),
        UNIQUE (tenant_id, webhook_secret_ref)
    )""")
    op.execute("ALTER TABLE scm_integrations ENABLE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_isolation ON scm_integrations
        USING (tenant_id = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON scm_integrations TO sca_api")


def downgrade() -> None:
    op.drop_table("scm_integrations")
