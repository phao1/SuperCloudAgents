from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE eval_runs (
        id uuid PRIMARY KEY, tenant_id text NOT NULL DEFAULT 'eval',
        suite text NOT NULL, suite_hash text NOT NULL, variant jsonb NOT NULL,
        created_by text NOT NULL, status text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz
    )""")
    op.execute("""CREATE TABLE eval_results (
        run_id uuid NOT NULL REFERENCES eval_runs(id), tenant_id text NOT NULL DEFAULT 'eval',
        case_id text NOT NULL, repeat integer NOT NULL, task_id uuid NOT NULL REFERENCES tasks(id),
        acceptance_passed boolean, verification_passed boolean, turns integer NOT NULL,
        tokens bigint NOT NULL, cost_usd numeric NOT NULL, duration_s double precision NOT NULL,
        PRIMARY KEY (run_id, case_id, repeat)
    )""")


def downgrade() -> None:
    op.drop_table("eval_results")
    op.drop_table("eval_runs")
