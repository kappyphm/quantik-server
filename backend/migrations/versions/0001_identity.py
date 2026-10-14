"""Identity, refresh rotation and audit persistence."""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("accounts", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(255), unique=True), sa.Column("google_sub", sa.String(255), unique=True),
        sa.Column("password_hash", sa.String(512)), sa.Column("role", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False), sa.CheckConstraint("role IN ('user', 'admin')"))
    op.create_table("refresh_sessions", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("previous_hash", sa.String(64)), sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False), sa.Column("generation", sa.Integer(), nullable=False))
    op.create_index("ix_refresh_sessions_account_id", "refresh_sessions", ["account_id"])
    op.create_index("ix_refresh_sessions_previous_hash", "refresh_sessions", ["previous_hash"])
    op.create_table("used_refresh_tokens", sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("refresh_sessions.id"), nullable=False))
    op.create_index("ix_used_refresh_tokens_session_id", "used_refresh_tokens", ["session_id"])
    op.create_table("login_throttles", sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("failures", sa.Integer(), nullable=False), sa.Column("blocked_until", sa.Integer(), nullable=False))
    op.create_table("audit_events", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("accounts.id")),
        sa.Column("action", sa.String(100), nullable=False), sa.Column("occurred_at", sa.Integer(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False))
    op.create_index("ix_audit_events_action", "audit_events", ["action"])


def downgrade() -> None:
    for table in ("audit_events", "login_throttles", "used_refresh_tokens", "refresh_sessions", "accounts"):
        op.drop_table(table)
