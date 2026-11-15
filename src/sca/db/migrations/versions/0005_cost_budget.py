import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("daily_cost_budget_usd", sa.Numeric()))


def downgrade() -> None:
    op.drop_column("tenants", "daily_cost_budget_usd")
