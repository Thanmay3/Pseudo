"""Sessions remain private until their owner explicitly publishes them."""
from alembic import op
import sqlalchemy as sa

revision = "0002_public_profiles"
down_revision = "0001_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("build_sessions", sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_check_constraint("only_completed_sessions_public", "build_sessions", "NOT is_published OR end_time IS NOT NULL")


def downgrade():
    raise RuntimeError("Downgrade would discard publishing preferences. Plan a manual rollback first.")
