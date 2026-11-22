"""Index the evolution conversation in the existing tenant-isolated journal."""

from alembic import op

revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None

CONVERSATION_EVENTS = "type IN ('conversation.message', 'conversation.progress')"


def upgrade() -> None:
    op.execute(f'''CREATE INDEX ix_evolution_conversation_stream
        ON evolution_events (tenant_id, (payload->>'evolution_id'), id)
        WHERE {CONVERSATION_EVENTS}''')
    op.execute(f'''CREATE UNIQUE INDEX ix_evolution_conversation_item
        ON evolution_events (tenant_id, (payload->>'evolution_id'), (payload->>'item_id'))
        WHERE {CONVERSATION_EVENTS}''')
    op.execute(f'''CREATE POLICY evolution_conversation_parent ON evolution_events
        AS RESTRICTIVE FOR INSERT TO sca_api WITH CHECK (NOT ({CONVERSATION_EVENTS}) OR EXISTS (
            SELECT 1 FROM evolutions WHERE evolutions.tenant_id = evolution_events.tenant_id
                AND evolutions.id::text = evolution_events.payload->>'evolution_id'
        ))''')


def downgrade() -> None:
    op.execute('DROP POLICY evolution_conversation_parent ON evolution_events')
    op.drop_index('ix_evolution_conversation_item')
    op.drop_index('ix_evolution_conversation_stream')
