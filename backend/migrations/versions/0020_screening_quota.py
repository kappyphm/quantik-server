"""Daily screening quota per plan (Free 5, Pro 20) and the usage ledger."""

from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("plan_versions", sa.Column("screening_quota", sa.Integer(),
        nullable=False, server_default="5"))
    with op.batch_alter_table("plan_versions") as batch_op:
        batch_op.drop_constraint("ck_plan_terms", type_="check")
        batch_op.create_check_constraint("ck_plan_terms",
            "price_vnd >= 0 AND duration_days > 0 AND analysis_quota > 0 AND screening_quota > 0 AND active_limit > 0")
    op.execute("UPDATE plan_versions SET screening_quota = 20 WHERE name = 'QuanTik Pro'")
    op.execute("UPDATE plan_versions SET screening_quota = 999999 WHERE name = 'Demo'")
    op.create_table(
        "screening_usages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("day_start", sa.Integer(), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("used >= 0"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "day_start"),
    )
    op.create_index("ix_screening_usages_account_id", "screening_usages", ["account_id"])


def downgrade():
    op.drop_index("ix_screening_usages_account_id", table_name="screening_usages")
    op.drop_table("screening_usages")
    with op.batch_alter_table("plan_versions") as batch_op:
        batch_op.drop_constraint("ck_plan_terms", type_="check")
        batch_op.create_check_constraint("ck_plan_terms",
            "price_vnd >= 0 AND duration_days > 0 AND analysis_quota > 0 AND active_limit > 0")
    op.drop_column("plan_versions", "screening_quota")
