"""Optional GitHub connections and explicitly selected public evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0003_github_evidence"
down_revision = "0002_public_profiles"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("projects", sa.Column("github_repo_id", sa.BigInteger(), nullable=True))
    op.create_table("github_connections",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("github_user_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("encrypted_token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("github_oauth_states",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("auth_session_hash", sa.String(64), sa.ForeignKey("auth_sessions.token_hash", ondelete="CASCADE"), nullable=False),
        sa.Column("encrypted_verifier", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_github_oauth_states_user_id", "github_oauth_states", ["user_id"])
    op.create_index("ix_github_oauth_states_expires_at", "github_oauth_states", ["expires_at"])
    op.create_table("github_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("build_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("repo_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("reference", sa.String(64), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "repo_id", "kind", "reference", name="unique_github_evidence"),
        sa.CheckConstraint("kind IN ('commit', 'pull_request')", name="valid_evidence_kind"))
    op.create_index("ix_github_evidence_session_id", "github_evidence", ["session_id"])


def downgrade():
    raise RuntimeError("Downgrade would discard GitHub connections/evidence. Plan a manual rollback first.")
