import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("events_archived_at", sa.DateTime(timezone=True)))
    op.add_column("tenants", sa.Column("daily_token_budget", sa.BigInteger()))

    op.execute("""
        CREATE FUNCTION public.sca_task_counts() RETURNS TABLE(state text, count bigint)
        LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog
        AS 'SELECT state, count(*) FROM public.tasks GROUP BY state'
    """)
    op.execute("REVOKE ALL ON FUNCTION public.sca_task_counts() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.sca_task_counts() TO sca_api")


def downgrade() -> None:
    op.execute("DROP FUNCTION public.sca_task_counts()")
    op.drop_column("tenants", "daily_token_budget")
    op.drop_column("tasks", "events_archived_at")
