"""Tenant administration, plans and daily usage."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0020"
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("monthly_cost_limit_usd", sa.Numeric),
        sa.Column("max_concurrent_tasks", sa.Integer),
        sa.Column("max_task_cost_usd", sa.Numeric),
        sa.Column("features", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint(
            "monthly_cost_limit_usd >= 0 AND monthly_cost_limit_usd < 'Infinity'::numeric"
        ),
        sa.CheckConstraint("max_concurrent_tasks > 0"),
        sa.CheckConstraint("max_task_cost_usd >= 0 AND max_task_cost_usd < 'Infinity'::numeric"),
    )
    op.execute("""INSERT INTO plans (name, monthly_cost_limit_usd, max_concurrent_tasks,
                                     max_task_cost_usd) VALUES
        ('free', 10, 1, 1), ('team', 100, 4, 10), ('enterprise', NULL, NULL, NULL)""")

    op.add_column("tenants", sa.Column("display_name", sa.Text))
    op.execute("UPDATE tenants SET display_name = id")
    op.alter_column("tenants", "display_name", nullable=False)
    op.add_column("tenants", sa.Column("plan", sa.Text, sa.ForeignKey("plans.name"),
                                       server_default="enterprise", nullable=False))
    op.add_column("tenants", sa.Column("status", sa.Text, server_default="active", nullable=False))
    op.add_column("tenants", sa.Column("membership_required", sa.Boolean, server_default=sa.false(),
                                       nullable=False))
    op.add_column("tenants", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.create_check_constraint("tenants_status", "tenants", "status IN ('active', 'suspended')")
    op.create_table(
        "tenant_members",
        sa.Column("tenant_id", sa.Text, sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("email", sa.Text, primary_key=True),
        sa.Column("subject", sa.Text),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "subject"),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member', 'viewer')"),
    )
    op.create_table(
        "usage_daily",
        sa.Column("tenant_id", sa.Text, sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("day", sa.Date, primary_key=True),
        *[sa.Column(name, sa.BigInteger, nullable=False, server_default="0") for name in
          ("tasks", "turns", "tokens_in", "tokens_out", "sandbox_seconds")],
        sa.Column("cost_usd", sa.Numeric, nullable=False, server_default="0"),
        sa.Column("by_model", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column("tasks", sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.add_column("tasks", sa.Column("billing_usage", JSONB))

    for table, tenant_column in (
        ("tenants", "id"), ("tenant_members", "tenant_id"), ("usage_daily", "tenant_id"),
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY tenant_isolation ON {table}
            USING ({tenant_column} = current_setting('app.tenant_id', true))
            WITH CHECK ({tenant_column} = current_setting('app.tenant_id', true))""")
    op.execute("GRANT SELECT ON plans, usage_daily TO sca_api")
    op.execute("GRANT SELECT, INSERT, UPDATE ON tenant_members TO sca_api")
    op.execute("REVOKE INSERT, UPDATE ON tenants FROM sca_api")
    op.execute("""GRANT INSERT (id, display_name, max_active_tasks, daily_token_budget,
        daily_cost_budget_usd), UPDATE (display_name, max_active_tasks, daily_token_budget,
        daily_cost_budget_usd) ON tenants TO sca_api""")

    # A provider such as Dex may omit the tenant claim. Only expose eligible tenant IDs.
    op.execute("""CREATE FUNCTION public.sca_oidc_member_tenants(p_subject text, p_email text)
        RETURNS SETOF text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $$
            SELECT m.tenant_id FROM public.tenant_members m
            JOIN public.tenants t ON t.id = m.tenant_id
            WHERE t.deleted_at IS NULL
              AND (m.subject = p_subject OR (m.subject IS NULL AND m.email = p_email))
            GROUP BY m.tenant_id
            ORDER BY m.tenant_id
            LIMIT 2
        $$""")
    op.execute("REVOKE ALL ON FUNCTION public.sca_oidc_member_tenants(text, text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.sca_oidc_member_tenants(text, text) TO sca_api")


def downgrade() -> None:
    op.execute("DROP FUNCTION public.sca_oidc_member_tenants(text, text)")
    op.drop_column("tasks", "billing_usage")
    op.drop_column("tasks", "finished_at")
    op.drop_table("usage_daily")
    op.drop_table("tenant_members")
    op.execute("DROP POLICY tenant_isolation ON tenants")
    op.execute("ALTER TABLE tenants DISABLE ROW LEVEL SECURITY")
    op.drop_constraint("tenants_status", "tenants", type_="check")
    for column in ("deleted_at", "membership_required", "status", "plan", "display_name"):
        op.drop_column("tenants", column)
    op.execute("GRANT INSERT, UPDATE ON tenants TO sca_api")
    op.drop_table("plans")
