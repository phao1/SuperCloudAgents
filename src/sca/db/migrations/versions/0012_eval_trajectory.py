"""Persist trajectory scoring without changing tenant policies."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("eval_results", sa.Column("trajectory_passed", sa.Boolean(), nullable=True))
    op.add_column("eval_results", sa.Column("trajectory_findings", JSONB(), nullable=False,
                                          server_default=sa.text("'[]'::jsonb")))
    op.execute("GRANT UPDATE (trajectory_passed, trajectory_findings) ON eval_results TO sca_api")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (trajectory_passed, trajectory_findings) ON eval_results FROM sca_api")
    op.drop_column("eval_results", "trajectory_findings")
    op.drop_column("eval_results", "trajectory_passed")
