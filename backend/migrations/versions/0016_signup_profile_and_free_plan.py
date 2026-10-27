"""Customer onboarding and once-only free subscriptions without payment records."""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("accounts", sa.Column("date_of_birth", sa.Date(), nullable=True))
    op.add_column("accounts", sa.Column("phone_number", sa.String(16), nullable=True))
    op.add_column("accounts", sa.Column("profile_required", sa.Boolean(), server_default=sa.false(), nullable=False))
    # Name the existing anonymous check for SQLite batch reflection as well.
    checks = sa.inspect(op.get_bind()).get_check_constraints("plan_versions")
    with op.batch_alter_table("plan_versions", naming_convention={"ck": "legacy_plan_terms"}) as batch:
        for check in checks:
            if "price_vnd" in check["sqltext"]:
                batch.drop_constraint(check["name"] or "legacy_plan_terms", type_="check")
        batch.create_check_constraint("ck_plan_terms", "price_vnd >= 0 AND duration_days > 0 AND analysis_quota > 0 AND active_limit > 0")
    with op.batch_alter_table("subscriptions") as batch:
        batch.alter_column("order_id", existing_type=sa.String(36), nullable=True)
        batch.alter_column("payment_id", existing_type=sa.String(32), nullable=True)
        batch.add_column(sa.Column("grant_key", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_subscription_grant", ["grant_key"])
        batch.create_check_constraint("ck_subscription_origin", "(order_id IS NOT NULL AND payment_id IS NOT NULL AND grant_key IS NULL) OR (order_id IS NULL AND payment_id IS NULL AND grant_key IS NOT NULL)")
    plan = sa.table("plan_versions", sa.column("id"), sa.column("name"), sa.column("price_vnd"), sa.column("duration_days"),
        sa.column("analysis_quota"), sa.column("active_limit"), sa.column("features", sa.JSON()), sa.column("available", sa.Boolean()), sa.column("created_at"))
    op.bulk_insert(plan, [{"id": "04dfe4bc-755c-4dc2-9891-b3c0e13c8001", "name": "Free", "price_vnd": 0,
        "duration_days": 30, "analysis_quota": 3, "active_limit": 1,
        "features": {"screening": True, "analysis": True}, "available": False, "created_at": 1792245600}])


def downgrade():
    raise RuntimeError("Free subscriptions must be migrated explicitly before downgrading billing schema")
