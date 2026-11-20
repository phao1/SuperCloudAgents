"""Store the OpenCode default selected by the release operator."""

from alembic import op

revision = '0017'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE platform_settings (
        tenant_id text PRIMARY KEY,
        opencode_default_version text NOT NULL,
        CONSTRAINT ck_platform_settings_operator CHECK (tenant_id = 'eval')
    )""")
    op.execute("ALTER TABLE platform_settings ENABLE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_isolation ON platform_settings
        USING (tenant_id = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""")
    op.execute("GRANT SELECT, INSERT, UPDATE ON platform_settings TO sca_api")


def downgrade() -> None:
    op.drop_table("platform_settings")
