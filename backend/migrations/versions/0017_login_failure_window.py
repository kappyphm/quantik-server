"""Expire failed login counters after the protection window."""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("login_throttles", sa.Column("last_failure_at", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("login_throttles", "last_failure_at")
