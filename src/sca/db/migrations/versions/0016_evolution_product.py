"""Champion rollouts, evolution schedules and benchmark harvests."""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

TABLES = ("champion_rollouts", "evolution_events", "evolution_schedules", "harvest_candidates")


def upgrade() -> None:
    op.execute("""CREATE TABLE champion_rollouts (
        tenant_id text NOT NULL, agent_version text NOT NULL, model text NOT NULL,
        champion_id uuid NOT NULL REFERENCES champions(id),
        percent integer NOT NULL CHECK (percent BETWEEN 0 AND 100),
        updated_by text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, agent_version, model)
    )""")
    op.execute("""CREATE TABLE evolution_events (
        id bigserial PRIMARY KEY, tenant_id text NOT NULL, type text NOT NULL, actor text NOT NULL,
        payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("""CREATE TABLE evolution_schedules (
        id uuid PRIMARY KEY, tenant_id text NOT NULL, suite text NOT NULL,
        split text NOT NULL DEFAULT 'dev' CHECK (split = 'dev'),
        agent_version text NOT NULL, model text NOT NULL,
        interval_hours double precision NOT NULL
            CHECK (interval_hours > 0 AND interval_hours < 'Infinity'::float8),
        config jsonb NOT NULL, threshold double precision NOT NULL CHECK (threshold BETWEEN 0 AND 1),
        always boolean NOT NULL DEFAULT false, enabled boolean NOT NULL DEFAULT true,
        auto_rollout_percent integer CHECK (auto_rollout_percent BETWEEN 0 AND 100),
        last_run_at timestamptz, last_eval_run_id uuid REFERENCES eval_runs(id),
        last_evolution_id uuid REFERENCES evolutions(id)
    )""")
    op.execute("""CREATE TABLE harvest_candidates (
        id uuid PRIMARY KEY, tenant_id text NOT NULL,
        task_id uuid NOT NULL REFERENCES tasks(id), suite text NOT NULL,
        split text NOT NULL CHECK (split IN ('dev', 'holdout')),
        case_id text NOT NULL, base_sha text NOT NULL, notes text NOT NULL,
        status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
        created_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        reviewed_by text, reviewed_at timestamptz
    )""")
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""")
        op.execute(f"GRANT SELECT, INSERT ON {table} TO sca_api")
        if table != "evolution_events":
            op.execute(f"GRANT UPDATE ON {table} TO sca_api")
    op.execute("GRANT DELETE ON evolution_schedules TO sca_api")
    op.execute("GRANT USAGE ON SEQUENCE evolution_events_id_seq TO sca_api")

    # A platform rollback rewrites references without exposing tenant rows to the API role.
    op.execute("""CREATE FUNCTION retarget_champion_rollouts(p_champion uuid, p_actor text)
    RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
    DECLARE changed_count bigint;
    BEGIN
        IF current_setting('app.tenant_id', true) IS DISTINCT FROM 'eval' THEN
            RAISE EXCEPTION 'Platform tenant required' USING ERRCODE = 'insufficient_privilege';
        END IF;
        WITH changed AS (
            UPDATE public.champion_rollouts AS rollout
            SET champion_id = parent.id, updated_by = p_actor, updated_at = now()
            FROM public.champions AS child JOIN public.champions AS parent
                ON parent.id = child.parent_champion_id AND parent.tenant_id = child.tenant_id
                AND parent.agent_version = child.agent_version AND parent.model = child.model
            WHERE child.id = p_champion AND child.tenant_id = 'eval'
                AND NOT child.active AND parent.active AND rollout.champion_id = child.id
            RETURNING rollout.tenant_id, rollout.agent_version, rollout.model,
                rollout.champion_id, rollout.percent
        )
        INSERT INTO public.evolution_events (tenant_id, type, actor, payload)
            SELECT tenant_id, 'rollout.rolled_back', p_actor, jsonb_build_object(
                'previous_champion_id', p_champion, 'champion_id', champion_id,
                'agent_version', agent_version, 'model', model, 'percent', percent)
            FROM changed;
        GET DIAGNOSTICS changed_count = ROW_COUNT;
        RETURN changed_count;
    END;
    $$""")
    op.execute("REVOKE ALL ON FUNCTION retarget_champion_rollouts(uuid, text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION retarget_champion_rollouts(uuid, text) TO sca_api")


def downgrade() -> None:
    op.execute("DROP FUNCTION retarget_champion_rollouts(uuid, text)")
    for table in reversed(TABLES):
        op.drop_table(table)
