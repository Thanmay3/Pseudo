"""Add accounts; preserve unowned Stage 3 data in archive tables."""
from alembic import op
import sqlalchemy as sa

revision = "0001_accounts"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    existing = sa.inspect(connection).get_table_names()
    if "projects" in existing:
        if "stage3_projects" in existing or "stage3_build_sessions" in existing:
            raise RuntimeError("Archive tables already exist; inspect before migrating.")
        if "user_id" in {c["name"] for c in sa.inspect(connection).get_columns("projects")}:
            raise RuntimeError("Owned projects already exist without a migration version; inspect before migrating.")
        # PostgreSQL updates the existing foreign key to the renamed table.
        op.rename_table("projects", "stage3_projects")
        op.rename_table("build_sessions", "stage3_build_sessions")
        # Index names are schema-wide. Preserve legacy indexes under new names.
        for table in ("stage3_projects", "stage3_build_sessions"):
            indexes = connection.execute(sa.text(
                "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema() AND tablename=:table"
            ), {"table": table}).scalars().all()
            for name in indexes:
                quote = connection.dialect.identifier_preparer.quote
                op.execute(sa.text(f"ALTER INDEX {quote(name)} RENAME TO {quote('archive_' + name)}"))

    op.create_table("users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("pseudonym", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("unique_pseudonym", "users", [sa.text("lower(pseudonym)")], unique=True)
    op.create_table("auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_table("projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_projects_user_id", "projects", ["user_id"])
    op.create_table("build_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task", sa.String(500), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True)),
        sa.Column("duration_seconds", sa.Integer()),
        sa.Column("accomplishment", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("(end_time IS NULL AND duration_seconds IS NULL) OR "
            "(end_time IS NOT NULL AND duration_seconds IS NOT NULL AND duration_seconds >= 0 AND end_time >= start_time)",
            name="valid_session_timing"))
    op.create_index("ix_build_sessions_project_id", "build_sessions", ["project_id"])


def downgrade():
    raise RuntimeError("Automatic downgrade would remove user data. Back up and plan a manual rollback instead.")
