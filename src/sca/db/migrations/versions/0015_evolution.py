"""Tenant-isolated evolution journal and champion registry."""

from alembic import op

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None

TABLES = ('evolutions', 'evolution_candidates', 'champions')


def upgrade() -> None:
    op.execute('''CREATE TABLE evolutions (
        id uuid PRIMARY KEY, tenant_id text NOT NULL, status text NOT NULL,
        config jsonb NOT NULL, created_by text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
        best_champion_id uuid, spent_usd numeric NOT NULL DEFAULT 0
    )''')
    op.execute('''CREATE TABLE evolution_candidates (
        id uuid PRIMARY KEY, tenant_id text NOT NULL,
        evolution_id uuid NOT NULL REFERENCES evolutions(id), generation integer NOT NULL,
        parent_id uuid, overlay jsonb NOT NULL, overlay_sha256 text NOT NULL,
        hypothesis text NOT NULL, diagnosis jsonb NOT NULL, static_check jsonb NOT NULL,
        dev_run_id uuid, holdout_run_id uuid, dev_metrics jsonb, holdout_metrics jsonb,
        decision text NOT NULL CHECK (decision IN ('pending','rejected','promoted','invalid')),
        reason text, created_at timestamptz NOT NULL DEFAULT now()
    )''')
    op.execute('''CREATE TABLE champions (
        id uuid PRIMARY KEY, tenant_id text NOT NULL, agent_version text NOT NULL,
        model text NOT NULL, overlay jsonb NOT NULL, overlay_sha256 text NOT NULL,
        candidate_id uuid REFERENCES evolution_candidates(id), metrics jsonb NOT NULL,
        parent_champion_id uuid, created_at timestamptz NOT NULL DEFAULT now(),
        active boolean NOT NULL DEFAULT false
    )''')
    op.execute('CREATE UNIQUE INDEX ix_champions_active ON champions (tenant_id, agent_version, model) WHERE active')
    for name in TABLES:
        op.execute(f'ALTER TABLE {name} ENABLE ROW LEVEL SECURITY')
        op.execute(f"""CREATE POLICY tenant_isolation ON {name}
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""")
        op.execute(f'GRANT SELECT, INSERT, UPDATE ON {name} TO sca_api')


def downgrade() -> None:
    for name in reversed(TABLES):
        op.drop_table(name)
