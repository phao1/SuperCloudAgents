"""Persist Stripe subscriptions, invoices, webhook receipts and monthly usage reports."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

TABLES = ("billing_customers", "billing_events", "billing_invoices", "billing_usage_reports")


def upgrade() -> None:
    op.add_column("plans", sa.Column("stripe_price_id", sa.Text))
    op.create_unique_constraint("plans_stripe_price_id_key", "plans", ["stripe_price_id"])
    op.add_column("plans", sa.Column("included_usage_usd", sa.Numeric))
    op.create_check_constraint(
        "plans_included_usage_usd", "plans",
        "included_usage_usd >= 0 AND included_usage_usd < 'Infinity'::numeric",
    )

    op.create_table(
        "billing_customers",
        sa.Column("tenant_id", sa.Text, sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("stripe_customer_id", sa.Text, nullable=False, unique=True),
        sa.Column("subscription_id", sa.Text),
        sa.Column("subscription_status", sa.Text),
        sa.Column("price_id", sa.Text),
        sa.Column("current_period_end", sa.DateTime(timezone=True)),
        sa.Column("suspended_for_nonpayment", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("checkout_session_id", sa.Text),
        sa.Column("checkout_price_id", sa.Text),
    )
    op.create_table(
        "billing_events",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("tenant_id", sa.Text, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("payload", JSONB, nullable=False),
    )
    op.create_index("ix_billing_events_tenant_received", "billing_events", ["tenant_id", "received_at"])
    op.create_table(
        "billing_invoices",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("subscription_id", sa.Text),
        sa.Column("number", sa.Text),
        sa.Column("amount_due", sa.BigInteger, nullable=False),
        sa.Column("amount_paid", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("attempt_count", sa.Integer, nullable=False),
        sa.Column("hosted_invoice_url", sa.Text),
        sa.Column("period_start", sa.DateTime(timezone=True)),
        sa.Column("period_end", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_billing_invoices_tenant_period", "billing_invoices", ["tenant_id", "period_end"])
    op.create_table(
        "billing_usage_reports",
        sa.Column("tenant_id", sa.Text, sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("month", sa.Date, primary_key=True),
        sa.Column("stripe_customer_id", sa.Text, nullable=False),
        sa.Column("subscription_id", sa.Text, nullable=False),
        sa.Column("amount_cents", sa.BigInteger, nullable=False),
        sa.Column("stripe_invoice_item_id", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""")
    op.execute("GRANT SELECT ON billing_customers, billing_invoices, billing_usage_reports TO sca_api")
    op.execute("GRANT SELECT (id, type, tenant_id, received_at) ON billing_events TO sca_api")
    op.execute("""GRANT INSERT (tenant_id, stripe_customer_id),
        UPDATE (checkout_session_id, checkout_price_id) ON billing_customers TO sca_api""")


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
    op.drop_column("plans", "included_usage_usd")
    op.drop_column("plans", "stripe_price_id")
