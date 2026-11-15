import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("role", sa.Text()),
        sa.Column("scopes", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("repo_url", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.execute("ALTER TABLE api_tokens ENABLE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_isolation ON api_tokens
        USING (tenant_id = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""")
    op.execute("GRANT SELECT, INSERT, UPDATE ON api_tokens TO sca_api")
    op.execute("""CREATE FUNCTION public.sca_token_lookup(hash text)
        RETURNS SETOF public.api_tokens
        LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog
        AS 'SELECT * FROM public.api_tokens WHERE token_hash = hash
            AND revoked_at IS NULL AND expires_at > now()'""")
    op.execute("REVOKE ALL ON FUNCTION public.sca_token_lookup(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.sca_token_lookup(text) TO sca_api")


def downgrade() -> None:
    op.execute("DROP FUNCTION public.sca_token_lookup(text)")
    op.drop_table("api_tokens")
