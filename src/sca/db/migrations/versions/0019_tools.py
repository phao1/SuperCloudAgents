"""MCP tool registry, tenant grants, and knowledge documents."""

from alembic import op

revision = "0019"
down_revision = '0017'
branch_labels = None
depends_on = None

TABLES = ("tools", "tool_grants", "kb_documents", "kb_chunks")
TENANT = "current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.execute("""CREATE TABLE tools (
        tenant_id text NOT NULL, name text NOT NULL, description text NOT NULL,
        kind text NOT NULL, url text, headers jsonb NOT NULL DEFAULT '{}'::jsonb,
        enabled boolean NOT NULL DEFAULT true, version integer NOT NULL DEFAULT 1,
        created_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, name),
        CHECK ((kind = 'remote' AND name <> 'knowledge' AND url IS NOT NULL)
            OR (kind = 'builtin' AND tenant_id = 'platform' AND name = 'knowledge'
                AND url IS NULL AND headers = '{}'::jsonb))
    )""")
    op.execute("""CREATE TABLE tool_grants (
        tenant_id text NOT NULL, tool_tenant_id text NOT NULL, tool_name text NOT NULL,
        subject_type text NOT NULL CHECK (subject_type IN ('tenant', 'team', 'user')),
        subject text NOT NULL, granted_by text NOT NULL,
        PRIMARY KEY (tenant_id, tool_tenant_id, tool_name, subject_type, subject),
        FOREIGN KEY (tool_tenant_id, tool_name) REFERENCES tools (tenant_id, name) ON DELETE CASCADE,
        CHECK (tool_tenant_id IN (tenant_id, 'platform')),
        CHECK (subject_type <> 'tenant' OR subject = tenant_id)
    )""")
    op.execute("""CREATE TABLE kb_documents (
        tenant_id text NOT NULL, id uuid NOT NULL, title text NOT NULL,
        format text NOT NULL CHECK (format IN ('text', 'markdown', 'pdf-text')),
        content text NOT NULL, allowed_teams text[] NOT NULL DEFAULT '{}',
        chunk_count integer NOT NULL, created_by text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (tenant_id, id)
    )""")
    op.execute("""CREATE TABLE kb_chunks (
        tenant_id text NOT NULL, document_id uuid NOT NULL, ordinal integer NOT NULL,
        heading text, content text NOT NULL, embedding_model text,
        PRIMARY KEY (tenant_id, document_id, ordinal),
        FOREIGN KEY (tenant_id, document_id) REFERENCES kb_documents (tenant_id, id) ON DELETE CASCADE
    )""")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
            ALTER TABLE kb_chunks ADD COLUMN embedding vector;
        END IF;
    END $$""")
    op.execute("""INSERT INTO tools (tenant_id, name, description, kind, created_by)
        VALUES ('platform', 'knowledge', '企业知识库', 'builtin', 'system')""")
    for name in TABLES:
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {name} TO sca_api")
    for name in ("tools", "kb_documents", "kb_chunks"):
        op.execute(f"""CREATE POLICY tenant_isolation ON {name}
            USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})""")
    op.execute(f"""CREATE POLICY platform_tools ON tools FOR SELECT
        USING (tenant_id = 'platform' AND nullif({TENANT}, '') IS NOT NULL)""")
    manages_grant = f"""(({TENANT} = 'platform' AND tool_tenant_id = 'platform') OR
        (tenant_id = {TENANT} AND (tool_tenant_id = tenant_id OR tool_name = 'knowledge')))"""
    op.execute(f"""CREATE POLICY tenant_grants ON tool_grants
        USING ({manages_grant}) WITH CHECK ({manages_grant})""")
    op.execute(f"""CREATE POLICY own_grant_visibility ON tool_grants FOR SELECT
        USING (tenant_id = {TENANT})""")


def downgrade() -> None:
    for name in reversed(TABLES):
        op.drop_table(name)
