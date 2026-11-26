"""billing_subscriptions

Premium subscriptions mirrored from Stripe (platform/billing.py): user_subscriptions, one row per user, and
billing_events, the Stripe event ids already handled. Additive only.

Revision ID: b8e2c4f6a1d3
Revises: a3c7e1f5d9b2
Create Date: 2026-11-26 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8e2c4f6a1d3"
down_revision: Union[str, Sequence[str], None] = "a3c7e1f5d9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "user_subscriptions" not in tables:
        op.create_table(
            "user_subscriptions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("stripe_customer_id", sa.String(length=64), nullable=True),
            sa.Column("stripe_subscription_id", sa.String(length=64), nullable=True),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="incomplete"),
            sa.Column("plan_interval", sa.String(length=8), nullable=True),
            sa.Column("price_id", sa.String(length=64), nullable=True),
            sa.Column("current_period_end", sa.DateTime(), nullable=True),
            sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("last_event_created", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("stripe_subscription_id", name="uq_user_subscriptions_stripe_subscription_id"),
        )
        op.create_index("ix_user_subscriptions_user_id", "user_subscriptions", ["user_id"], unique=True)
        op.create_index("ix_user_subscriptions_stripe_customer_id", "user_subscriptions", ["stripe_customer_id"])
    if "billing_events" not in tables:
        op.create_table(
            "billing_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("stripe_event_id", sa.String(length=80), nullable=False),
            sa.Column("event_type", sa.String(length=80), nullable=False),
            sa.Column("received_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("stripe_event_id", name="uq_billing_events_stripe_event_id"),
        )


def downgrade() -> None:
    op.drop_table("billing_events")
    op.drop_index("ix_user_subscriptions_stripe_customer_id", table_name="user_subscriptions")
    op.drop_index("ix_user_subscriptions_user_id", table_name="user_subscriptions")
    op.drop_table("user_subscriptions")
