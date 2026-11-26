"""
Premium billing through Stripe Checkout (subscriptions), TEST MODE by default.

How it fits together:
  1. POST /billing/checkout makes a Stripe Checkout Session for the monthly or yearly price and returns its URL; the
     browser goes to Stripe's own hosted page, so no card ever touches this app.
  2. Stripe calls POST /billing/webhook. The signature is checked with STRIPE_WEBHOOK_SECRET, then the event updates
     user_subscriptions. That row, not the browser coming back, is what makes a user premium.
  3. platform/entitlements.py asks is_premium(); premium users get every feature in PREMIUM_FEATURES there.

Settings (environment variable names only, values live in Vercel / .env, never in the repo):
  STRIPE_SECRET_KEY              sk_test_... (a live key is refused unless STRIPE_LIVE_MODE_APPROVED=yes)
  STRIPE_WEBHOOK_SECRET          whsec_... from the webhook endpoint in the Stripe dashboard
  STRIPE_PRICE_PREMIUM_MONTHLY   price_... for the monthly plan
  STRIPE_PRICE_PREMIUM_YEARLY    price_... for the yearly plan
  FSC_PUBLIC_BASE_URL            optional, e.g. https://fantasimulator.com, for the return links (else the request's host)
  STRIPE_LIVE_MODE_APPROVED      only "yes" allows live keys and live events; leave unset for test mode

Stripe is called over its REST API with the standard library, so there is no extra package to install.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.billing import BillingEvent, UserSubscription
from fantasy_sports.platform.db.models.core import User

STRIPE_API = "https://api.stripe.com/v1"
SIGNATURE_TOLERANCE_SECONDS = 300

# Display prices for the pricing page. The amount actually charged is whatever the Stripe price ids say, so change
# both together. From the AVL-128 premium research (per user, all sports): below Yahoo Plus's $39.99 a year.
PLANS = {
    "month": {"label": "Monthly", "price": "$2.99", "per": "month", "env": "STRIPE_PRICE_PREMIUM_MONTHLY"},
    "year": {"label": "Yearly", "price": "$29.99", "per": "year", "env": "STRIPE_PRICE_PREMIUM_YEARLY"},
}

# Stripe statuses that keep premium on. past_due stays on while Stripe retries the card; canceled/unpaid end it.
PREMIUM_STATUSES = {"active", "trialing", "past_due"}


class BillingError(Exception):
    """Something the caller should show plainly (never contains a key)."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# ---- settings -----------------------------------------------------------------------------------------------------

def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def live_mode_approved() -> bool:
    return _env("STRIPE_LIVE_MODE_APPROVED").lower() == "yes"


def missing_settings() -> list[str]:
    """Names of the settings checkout still needs (names only, never values)."""
    names = ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"] + [p["env"] for p in PLANS.values()]
    return [n for n in names if not _env(n)]


def key_mode() -> str:
    """test, live or none, read from the key's prefix."""
    key = _env("STRIPE_SECRET_KEY")
    if key.startswith(("sk_test_", "rk_test_")):
        return "test"
    if key.startswith(("sk_live_", "rk_live_")):
        return "live"
    return "none" if not key else "unknown"


def _secret_key() -> str:
    mode = key_mode()
    if mode == "none":
        raise BillingError("Checkout isn't set up yet (STRIPE_SECRET_KEY is missing).", 503)
    if mode == "live" and not live_mode_approved():
        raise BillingError("A live Stripe key is set but live mode isn't approved; checkout stays off.", 503)
    if mode == "unknown":
        raise BillingError("STRIPE_SECRET_KEY doesn't look like a Stripe secret key.", 503)
    return _env("STRIPE_SECRET_KEY")


def checkout_ready() -> bool:
    if missing_settings():
        return False
    return key_mode() == "test" or (key_mode() == "live" and live_mode_approved())


# ---- Stripe REST calls ----------------------------------------------------------------------------------------------

def _flatten(params: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: nested dicts and lists become key[sub][0]=value."""
    out: list[tuple[str, str]] = []
    for k, v in params.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out += _flatten(v, key)
        elif isinstance(v, (list, tuple)):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    out += _flatten(item, f"{key}[{i}]")
                else:
                    out.append((f"{key}[{i}]", str(item)))
        elif isinstance(v, bool):
            out.append((key, "true" if v else "false"))
        elif v is not None:
            out.append((key, str(v)))
    return out


def stripe_post(path: str, params: dict) -> dict:
    """POST to Stripe. Kept as one function so the smoke test can replace it and never reach the network."""
    body = urllib.parse.urlencode(_flatten(params)).encode()
    req = urllib.request.Request(f"{STRIPE_API}{path}", data=body, method="POST", headers={
        "Authorization": f"Bearer {_secret_key()}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode()).get("error", {}).get("message") or str(exc)
        except Exception:  # noqa: BLE001
            message = str(exc)
        raise BillingError(f"Stripe refused the request: {message}", 502) from None
    except urllib.error.URLError:
        raise BillingError("Couldn't reach Stripe; try again in a minute.", 502) from None


# ---- checkout and the customer portal -----------------------------------------------------------------------------

def subscription_for(db: Session, user_id: int) -> UserSubscription | None:
    return db.query(UserSubscription).filter_by(user_id=user_id).first()


def is_premium(db: Session, user_id: int) -> bool:
    sub = subscription_for(db, user_id)
    return sub is not None and sub.status in PREMIUM_STATUSES


def create_checkout_session(db: Session, user: User, interval: str, base_url: str) -> str:
    if interval not in PLANS:
        raise BillingError("Pick the monthly or the yearly plan.")
    price_id = _env(PLANS[interval]["env"])
    if not price_id:
        raise BillingError(f"Checkout isn't set up yet ({PLANS[interval]['env']} is missing).", 503)
    if is_premium(db, user.id):
        raise BillingError("You already have Premium. Use Manage subscription to change it.", 409)
    base = (_env("FSC_PUBLIC_BASE_URL") or base_url).rstrip("/")
    params: dict = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": f"{base}/static/pricing.html?checkout=success",
        "cancel_url": f"{base}/static/pricing.html?checkout=cancelled",
        "client_reference_id": str(user.id),
        "metadata": {"user_id": str(user.id)},
        "subscription_data": {"metadata": {"user_id": str(user.id)}},
        "allow_promotion_codes": True,
    }
    sub = subscription_for(db, user.id)
    if sub is not None and sub.stripe_customer_id:
        params["customer"] = sub.stripe_customer_id            # same Stripe customer on a re-subscribe
    elif user.email and "@" in user.email:
        params["customer_email"] = user.email
    session = stripe_post("/checkout/sessions", params)
    url = session.get("url")
    if not url:
        raise BillingError("Stripe didn't return a checkout page.", 502)
    return url


def create_portal_session(db: Session, user: User, base_url: str) -> str:
    """Stripe's hosted page where a subscriber cancels, switches plan or updates the card."""
    sub = subscription_for(db, user.id)
    if sub is None or not sub.stripe_customer_id:
        raise BillingError("There's no subscription on this account yet.", 404)
    base = (_env("FSC_PUBLIC_BASE_URL") or base_url).rstrip("/")
    portal = stripe_post("/billing_portal/sessions", {
        "customer": sub.stripe_customer_id, "return_url": f"{base}/static/pricing.html",
    })
    if not portal.get("url"):
        raise BillingError("Stripe didn't return the subscription page.", 502)
    return portal["url"]


# ---- the webhook --------------------------------------------------------------------------------------------------

def verify_signature(payload: bytes, header: str | None, secret: str, now: float | None = None) -> None:
    """Stripe-Signature check (t=<time>,v1=<hmac sha256 of "<t>.<raw body>">). Raises BillingError(400) on failure."""
    if not secret:
        raise BillingError("Webhook isn't set up (STRIPE_WEBHOOK_SECRET is missing).", 503)
    if not header:
        raise BillingError("Missing Stripe-Signature header.")
    parts = [p.split("=", 1) for p in header.split(",") if "=" in p]
    timestamps = [v for k, v in parts if k.strip() == "t"]
    signatures = [v for k, v in parts if k.strip() == "v1"]
    if not timestamps or not signatures:
        raise BillingError("Malformed Stripe-Signature header.")
    try:
        t = int(timestamps[0])
    except ValueError:
        raise BillingError("Malformed Stripe-Signature header.") from None
    expected = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s.strip()) for s in signatures):
        raise BillingError("Stripe signature doesn't match.")
    if abs((now if now is not None else time.time()) - t) > SIGNATURE_TOLERANCE_SECONDS:
        raise BillingError("Stripe signature is too old.")


def _ts(value) -> datetime.datetime | None:
    if not value:
        return None
    return datetime.datetime.fromtimestamp(int(value), tz=datetime.timezone.utc).replace(tzinfo=None)


def _user_id_from(obj: dict) -> int | None:
    raw = (obj.get("metadata") or {}).get("user_id") or obj.get("client_reference_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _find_row(db: Session, obj: dict, subscription_id: str | None, customer_id: str | None) -> UserSubscription | None:
    """The row this Stripe object belongs to: by our user id in its metadata, else by subscription id, else customer."""
    user_id = _user_id_from(obj)
    if user_id is not None:
        if db.get(User, user_id) is None:
            return None
        row = subscription_for(db, user_id)
        if row is None:
            row = UserSubscription(user_id=user_id, status="incomplete", last_event_created=0, cancel_at_period_end=False)
            db.add(row)
        return row
    if subscription_id:
        row = db.query(UserSubscription).filter_by(stripe_subscription_id=subscription_id).first()
        if row is not None:
            return row
    if customer_id:
        return db.query(UserSubscription).filter_by(stripe_customer_id=customer_id).first()
    return None


def _apply_subscription(row: UserSubscription, sub: dict) -> None:
    row.stripe_subscription_id = sub.get("id") or row.stripe_subscription_id
    row.stripe_customer_id = sub.get("customer") or row.stripe_customer_id
    row.status = sub.get("status") or row.status
    row.cancel_at_period_end = bool(sub.get("cancel_at_period_end"))
    items = ((sub.get("items") or {}).get("data") or [])
    first = items[0] if items else {}
    price = first.get("price") or {}
    row.price_id = price.get("id") or row.price_id
    interval = (price.get("recurring") or {}).get("interval")
    row.plan_interval = interval or row.plan_interval
    # Newer Stripe API versions keep the period on the item; older ones on the subscription.
    row.current_period_end = _ts(first.get("current_period_end") or sub.get("current_period_end")) or row.current_period_end


def handle_event(db: Session, event: dict) -> str:
    """Apply one verified Stripe event. Returns a short word for the log/response. Caller commits."""
    event_id = event.get("id") or ""
    event_type = event.get("type") or ""
    if event.get("livemode") and not live_mode_approved():
        return "ignored_live_event"
    if event_id and db.query(BillingEvent).filter_by(stripe_event_id=event_id).first() is not None:
        return "duplicate"
    obj = (event.get("data") or {}).get("object") or {}
    created = int(event.get("created") or 0)
    result = "ignored"

    if event_type == "checkout.session.completed" and obj.get("mode") == "subscription":
        row = _find_row(db, obj, obj.get("subscription"), obj.get("customer"))
        if row is not None:
            row.stripe_customer_id = obj.get("customer") or row.stripe_customer_id
            row.stripe_subscription_id = obj.get("subscription") or row.stripe_subscription_id
            # The subscription events carry the real status; only lift a row that hasn't heard from them yet.
            if obj.get("payment_status") in ("paid", "no_payment_required") and row.status == "incomplete":
                row.status = "active"
            result = "linked"
        else:
            result = "unknown_user"

    elif event_type in ("customer.subscription.created", "customer.subscription.updated",
                        "customer.subscription.deleted"):
        row = _find_row(db, obj, obj.get("id"), obj.get("customer"))
        if row is None:
            result = "unknown_user"
        elif created and created < (row.last_event_created or 0):
            result = "stale"                                   # an older event arriving late never undoes a newer one
        else:
            _apply_subscription(row, obj)
            if event_type == "customer.subscription.deleted":
                row.status = "canceled"
            row.last_event_created = max(created, row.last_event_created or 0)
            result = "updated"

    if event_id:
        db.add(BillingEvent(stripe_event_id=event_id, event_type=event_type[:80]))
    db.flush()
    return result


def status_for(db: Session, user_id: int) -> dict:
    sub = subscription_for(db, user_id)
    return {
        "premium": sub is not None and sub.status in PREMIUM_STATUSES,
        "status": sub.status if sub else "none",
        "plan_interval": sub.plan_interval if sub else None,
        "current_period_end": sub.current_period_end.isoformat() if sub and sub.current_period_end else None,
        "cancel_at_period_end": bool(sub.cancel_at_period_end) if sub else False,
        "can_manage": bool(sub and sub.stripe_customer_id),
    }
