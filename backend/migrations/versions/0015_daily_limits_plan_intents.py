"""Daily admission limits and idempotent plan versions; additive migration."""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    sqlite = op.get_bind().dialect.name == "sqlite"
    for table, name, target in (("plan_versions", "created_by", "accounts"), ("plan_versions", "replaces_plan_id", "plan_versions"), ("quota_buckets", "subscription_id", "subscriptions")):
        if sqlite:
            op.execute(f"ALTER TABLE {table} ADD COLUMN {name} VARCHAR(36) REFERENCES {target}(id)")
        else:
            op.add_column(table, sa.Column(name, sa.String(36), sa.ForeignKey(f"{target}.id", name=f"fk_{table}_{name}"), nullable=True))
    op.add_column("plan_versions", sa.Column("creation_key", sa.String(128), nullable=True))
    op.add_column("plan_versions", sa.Column("creation_fingerprint", sa.String(64), nullable=True))
    op.add_column("quota_buckets", sa.Column("daily_created", sa.Integer(), server_default="0", nullable=False))
    op.create_index("uq_plan_creation_intent", "plan_versions", ["created_by", "creation_key"], unique=True)
    op.create_index("uq_subscription_day", "quota_buckets", ["subscription_id", "valid_from"], unique=True)
    op.create_table("purchase_intents",
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), primary_key=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("purchase_orders.id"), nullable=False))


def downgrade():
    op.drop_table("purchase_intents")
    op.drop_index("uq_subscription_day", table_name="quota_buckets")
    op.drop_index("uq_plan_creation_intent", table_name="plan_versions")
    for table, names in (("quota_buckets", ("daily_created", "subscription_id")), ("plan_versions", ("creation_fingerprint", "creation_key", "replaces_plan_id", "created_by"))):
        for name in names:
            op.drop_column(table, name)
