"""Let tenant admins remove members."""

from alembic import op

revision = "0021"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT DELETE ON tenant_members TO sca_api")


def downgrade() -> None:
    op.execute("REVOKE DELETE ON tenant_members FROM sca_api")
