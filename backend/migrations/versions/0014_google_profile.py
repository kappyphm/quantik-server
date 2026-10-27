"""Retain verified Google name and email for customer profile display."""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("display_name", sa.String(255), nullable=True))
    op.add_column("accounts", sa.Column("email", sa.String(320), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "email")
    op.drop_column("accounts", "display_name")
