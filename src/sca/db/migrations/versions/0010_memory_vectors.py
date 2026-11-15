from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
            CREATE EXTENSION IF NOT EXISTS vector;
            ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding vector;
        END IF;
    END $$""")


def downgrade() -> None:
    op.execute("ALTER TABLE memories DROP COLUMN IF EXISTS embedding")
