from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.sca_oldest_active_task_age() RETURNS double precision
        LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog
        AS $$ SELECT COALESCE(GREATEST(EXTRACT(EPOCH FROM
            (CURRENT_TIMESTAMP - MIN(created_at))), 0), 0)::double precision
            FROM public.tasks WHERE state IN ('PROVISIONING', 'RUNNING') $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.sca_oldest_active_task_age() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.sca_oldest_active_task_age() TO sca_api")


def downgrade() -> None:
    op.execute("DROP FUNCTION public.sca_oldest_active_task_age()")
