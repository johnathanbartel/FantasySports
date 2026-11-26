"""Free and Premium plans, Stripe Checkout and the Stripe webhook (see platform/billing.py)."""

import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import current_user, optional_user
from fantasy_sports.platform import billing, entitlements
from fantasy_sports.platform.db.models.core import User
from fantasy_sports.platform.db.session import get_db

router = APIRouter(prefix="/billing", tags=["billing"])

FREE_FEATURES = [
    "Create and join leagues in every sport",
    "Snake and auction drafts, mock drafts and offline drafts",
    "Lineups, waivers and FAAB, trades, standings and playoffs",
    "EPA football scoring and xG hockey scoring",
    "League chat, draft board and power rankings",
]
PREMIUM_FEATURES = [
    "Everything in Free",
    "Commissioner Mode: run all-AI leagues and act for AI teams",
    "AI draft grades and pick values",
    "One subscription covers every sport",
]


def _maybe_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> User | None:
    """The signed-in user if the token is good; a stale token just shows the page signed out."""
    try:
        return optional_user(authorization, db)
    except HTTPException:
        return None


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.get("/plans")
def plans(db: Session = Depends(get_db), user: User | None = Depends(_maybe_user)):
    """The pricing page: what's free, what's premium, the prices, and whether checkout is switched on."""
    return {
        "free": {"name": "Free", "price": "$0", "features": FREE_FEATURES},
        "premium": {
            "name": "Premium",
            "features": PREMIUM_FEATURES,
            "prices": [{"interval": k, "label": v["label"], "price": v["price"], "per": v["per"]}
                       for k, v in billing.PLANS.items()],
        },
        "checkout_ready": billing.checkout_ready(),
        "test_mode": billing.key_mode() != "live",
        "me": billing.status_for(db, user.id) if user else None,
    }


@router.get("/me")
def my_billing(db: Session = Depends(get_db), user: User = Depends(current_user)):
    out = billing.status_for(db, user.id)
    out["granted_to_everyone_in_dev"] = entitlements.grant_all_in_dev()
    return out


class CheckoutIn(BaseModel):
    interval: str = "month"            # month | year


def _call(fn, *args):
    try:
        return fn(*args)
    except billing.BillingError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


@router.post("/checkout")
def start_checkout(body: CheckoutIn, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    """Returns Stripe's hosted checkout page URL; the browser goes there."""
    return {"url": _call(billing.create_checkout_session, db, user, body.interval, _base_url(request))}


@router.post("/portal")
def manage_subscription(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Stripe's hosted page to cancel, switch plan or update the card."""
    return {"url": _call(billing.create_portal_session, db, user, _base_url(request))}


@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, stripe_signature: str | None = Header(default=None),
                         db: Session = Depends(get_db)):
    """Stripe calls this. Only a correctly signed event changes anything."""
    payload = await request.body()
    try:
        billing.verify_signature(payload, stripe_signature, billing._env("STRIPE_WEBHOOK_SECRET"))
    except billing.BillingError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    try:
        event = json.loads(payload.decode())
    except ValueError:
        raise HTTPException(400, "Webhook body isn't JSON") from None
    result = billing.handle_event(db, event)
    db.commit()
    return {"received": True, "result": result}
