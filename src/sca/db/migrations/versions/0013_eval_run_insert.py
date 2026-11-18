"""Let the API create the eval run row before it starts the workflow, and fail it if that start fails."""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT INSERT ON eval_runs TO sca_api")
    op.execute("GRANT UPDATE (status, finished_at) ON eval_runs TO sca_api")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (status, finished_at) ON eval_runs FROM sca_api")
    op.execute("REVOKE INSERT ON eval_runs FROM sca_api")
