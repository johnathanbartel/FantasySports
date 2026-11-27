"""
Billing tables (platform/billing.py): one premium subscription per user, mirrored from Stripe by the webhook, and the
Stripe event ids already handled so a resent event is applied once. No card data is ever stored here; Stripe keeps it.
"""

from __future__ import annotations

import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from fantasy_sports.platform.db.base import Base


class UserSubscription(Base):
    """A user's premium subscription as Stripe last reported it. status is Stripe's own word (active, trialing,
    past_due, canceled, unpaid, incomplete, ...); platform/billing.py decides which of them count as premium."""

    __tablename__ = "user_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(24), default="incomplete")
    plan_interval: Mapped[str | None] = mapped_column(String(8), nullable=True)        # month | year
    price_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_period_end: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    last_event_created: Mapped[int] = mapped_column(Integer, default=0)                 # Stripe event time: older events are ignored
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class BillingEvent(Base):
    """A Stripe webhook event already processed (Stripe may send the same event more than once)."""

    __tablename__ = "billing_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    stripe_event_id: Mapped[str] = mapped_column(String(80), unique=True)
    event_type: Mapped[str] = mapped_column(String(80))
    received_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
