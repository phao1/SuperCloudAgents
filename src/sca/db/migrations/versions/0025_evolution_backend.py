"""Key evolution champions, rollouts and schedules by agent backend."""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

TABLES = ("champions", "champion_rollouts", "evolution_schedules")


def _replace_rollback_function(*, backend_aware: bool) -> None:
    parent_backend = "AND parent.agent_backend = child.agent_backend" if backend_aware else ""
    rollout_backend = "AND rollout.agent_backend = child.agent_backend" if backend_aware else ""
    backend_column = ", rollout.agent_backend" if backend_aware else ""
    backend_event = "'agent_backend', agent_backend, " if backend_aware else ""
    op.execute(f"""CREATE OR REPLACE FUNCTION retarget_champion_rollouts(p_champion uuid, p_actor text)
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
                {parent_backend}
            WHERE child.id = p_champion AND child.tenant_id = 'eval'
                AND NOT child.active AND parent.active AND rollout.champion_id = child.id
                {rollout_backend}
            RETURNING rollout.tenant_id, rollout.agent_version, rollout.model,
                rollout.champion_id, rollout.percent{backend_column}
        )
        INSERT INTO public.evolution_events (tenant_id, type, actor, payload)
            SELECT tenant_id, 'rollout.rolled_back', p_actor, jsonb_build_object(
                'previous_champion_id', p_champion, 'champion_id', champion_id,
                {backend_event}'agent_version', agent_version, 'model', model, 'percent', percent)
            FROM changed;
        GET DIAGNOSTICS changed_count = ROW_COUNT;
        RETURN changed_count;
    END;
    $$""")


def _rekey(*, backend_aware: bool) -> None:
    key = "tenant_id, agent_backend, agent_version, model" if backend_aware else "tenant_id, agent_version, model"
    op.execute("DROP INDEX ix_champions_active")
    op.execute(f"CREATE UNIQUE INDEX ix_champions_active ON champions ({key}) WHERE active")
    op.execute("ALTER TABLE champion_rollouts DROP CONSTRAINT champion_rollouts_pkey")
    op.execute(f"ALTER TABLE champion_rollouts ADD PRIMARY KEY ({key})")


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"""ALTER TABLE {table} ADD COLUMN agent_backend text NOT NULL DEFAULT 'opencode'
            CONSTRAINT {table}_agent_backend_check CHECK (agent_backend IN ('opencode', 'dsh'))""")
    _rekey(backend_aware=True)
    _replace_rollback_function(backend_aware=True)


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM champions WHERE agent_backend = 'dsh')
            OR EXISTS (SELECT 1 FROM champion_rollouts WHERE agent_backend = 'dsh')
            OR EXISTS (SELECT 1 FROM evolution_schedules WHERE agent_backend = 'dsh')
            OR EXISTS (SELECT 1 FROM evolutions WHERE config->>'agent_backend' = 'dsh') THEN
            RAISE EXCEPTION 'Revision 0024 cannot represent DeepSeek Harness evolution data';
        END IF;
    END $$""")
    _replace_rollback_function(backend_aware=False)
    _rekey(backend_aware=False)
    for table in TABLES:
        op.drop_column(table, "agent_backend")
