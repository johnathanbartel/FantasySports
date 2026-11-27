"""
Premium billing (platform/billing.py), offline, on a throwaway SQLite file (billing_smoke_test.db). Stripe is never
called: billing.stripe_post is replaced by a fake that records what would have been sent. Checks the plans endpoint,
checkout (monthly/yearly, settings missing, live keys refused, already premium), the webhook (signature, replay,
idempotency, out-of-order events, cancel, unknown users, live events ignored), the customer portal, and the Premium
gate on Commissioner Mode and draft grades.

    .venv-api/Scripts/python.exe scripts/seed_billing_smoke_test.py
"""

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

db_path = Path(os.environ.get("BILLING_SMOKE_DB", "billing_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
os.environ.pop("FSC_GRANT_ALL_PREMIUM", None)
os.environ.pop("STRIPE_LIVE_MODE_APPROVED", None)
for name in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_PREMIUM_MONTHLY", "STRIPE_PRICE_PREMIUM_YEARLY",
             "FSC_PUBLIC_BASE_URL"):
    os.environ.pop(name, None)
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient  # noqa: E402

from fantasy_sports.api.main import app  # noqa: E402
from fantasy_sports.platform import billing, entitlements  # noqa: E402
from fantasy_sports.platform.db.base import Base  # noqa: E402
from fantasy_sports.platform.db.models.billing import BillingEvent, UserSubscription  # noqa: E402
from fantasy_sports.platform.db.models.core import User  # noqa: E402
from fantasy_sports.platform.db.session import SessionLocal, engine  # noqa: E402

Base.metadata.create_all(bind=engine)
client = TestClient(app)
checks = 0


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


sent: list[tuple[str, dict]] = []


def fake_stripe_post(path, params):
    billing._secret_key()                              # the same key checks the real call makes
    sent.append((path, params))
    if path == "/checkout/sessions":
        return {"id": "cs_test_1", "url": "https://checkout.stripe.test/c/pay/cs_test_1"}
    if path == "/billing_portal/sessions":
        return {"id": "bps_test_1", "url": "https://billing.stripe.test/p/session/bps_test_1"}
    raise AssertionError(path)


billing.stripe_post = fake_stripe_post

WHSEC = "whsec_smoke_only"


def sign(body: bytes, secret=WHSEC, t=None):
    t = int(time.time()) if t is None else t
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def send(event, secret=WHSEC, t=None, header=None):
    body = json.dumps(event).encode()
    return client.post("/billing/webhook", content=body,
                       headers={"Stripe-Signature": header if header is not None else sign(body, secret, t),
                                "Content-Type": "application/json"})


def sub_event(eid, etype, created, user_id, status, sub_id="sub_test_1", cust="cus_test_1", interval="month",
              cancel=False, period_end=None):
    obj = {"id": sub_id, "object": "subscription", "customer": cust, "status": status, "cancel_at_period_end": cancel,
           "metadata": {"user_id": str(user_id)} if user_id is not None else {},
           "items": {"data": [{"price": {"id": f"price_test_{interval}", "recurring": {"interval": interval}},
                               "current_period_end": period_end or created + 30 * 86400}]}}
    return {"id": eid, "type": etype, "created": created, "livemode": False, "data": {"object": obj}}


db = SessionLocal()
alice = User(email="alice@example.test", display_name="Alice")
bob = User(email="bob@example.test", display_name="Bob")
db.add_all([alice, bob])
db.commit()
A, B = alice.id, bob.id
tok_a = client.post("/sessions", json={"user_id": A}).json()["token"]
tok_b = client.post("/sessions", json={"user_id": B}).json()["token"]
HA = {"Authorization": f"Bearer {tok_a}"}
HB = {"Authorization": f"Bearer {tok_b}"}

# ---- plans and settings ---------------------------------------------------------------------------------------------
p = client.get("/billing/plans").json()
check(p["checkout_ready"] is False and p["me"] is None, "no settings: checkout off, signed out")
check([x["price"] for x in p["premium"]["prices"]] == ["$2.99", "$29.99"], "monthly and yearly display prices")
check(client.get("/billing/plans", headers={"Authorization": "Bearer stale"}).status_code == 200, "a stale token still sees the plans")
check(set(billing.missing_settings()) == {"STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_PREMIUM_MONTHLY",
                                          "STRIPE_PRICE_PREMIUM_YEARLY"}, "missing settings named")
r = client.post("/billing/checkout", json={"interval": "month"}, headers=HA)
check(r.status_code == 503 and "STRIPE_PRICE_PREMIUM_MONTHLY" in r.json()["detail"], "checkout without a price id: 503 naming it")
check(client.post("/billing/checkout", json={"interval": "month"}).status_code == 401, "checkout needs sign-in")

os.environ.update({"STRIPE_PRICE_PREMIUM_MONTHLY": "price_test_month", "STRIPE_PRICE_PREMIUM_YEARLY": "price_test_year",
                   "STRIPE_WEBHOOK_SECRET": WHSEC})
os.environ["STRIPE_SECRET_KEY"] = "sk_live_" + "x" * 10
check(billing.checkout_ready() is False, "live key: checkout stays off")
r = client.post("/billing/checkout", json={"interval": "month"}, headers=HA)
check(r.status_code == 503 and "live" in r.json()["detail"].lower() and "sk_live" not in r.json()["detail"],
      "live key refused without approval, key not echoed")
check(not sent, "nothing sent to Stripe with a live key")
os.environ["STRIPE_SECRET_KEY"] = "sk_test_" + "x" * 10
check(billing.checkout_ready() is True and client.get("/billing/plans").json()["test_mode"] is True, "test key: checkout on")

# ---- checkout ---------------------------------------------------------------------------------------------------------
check(client.post("/billing/checkout", json={"interval": "week"}, headers=HA).status_code == 400, "unknown interval: 400")
r = client.post("/billing/checkout", json={"interval": "year"}, headers=HA)
check(r.status_code == 200 and r.json()["url"].startswith("https://checkout.stripe.test/"), "checkout returns Stripe's page")
path, params = sent[-1]
check(path == "/checkout/sessions" and params["mode"] == "subscription", "subscription checkout")
check(params["line_items"] == [{"price": "price_test_year", "quantity": 1}], "yearly price id used")
check(params["client_reference_id"] == str(A) and params["subscription_data"]["metadata"]["user_id"] == str(A),
      "user id carried to the session and the subscription")
check(params["customer_email"] == "alice@example.test", "email prefilled for a new customer")
check(params["success_url"].endswith("/static/pricing.html?checkout=success") and params["cancel_url"].endswith("checkout=cancelled"),
      "return links to the pricing page")
os.environ["FSC_PUBLIC_BASE_URL"] = "https://fantasimulator.example/"
client.post("/billing/checkout", json={"interval": "month"}, headers=HA)
check(sent[-1][1]["success_url"] == "https://fantasimulator.example/static/pricing.html?checkout=success",
      "FSC_PUBLIC_BASE_URL sets the return links")
os.environ.pop("FSC_PUBLIC_BASE_URL")

# ---- the gate before paying -------------------------------------------------------------------------------------------
check(entitlements.has_feature(db, alice, entitlements.COMMISSIONER_MODE) is False, "no premium yet: commissioner mode off")
check(entitlements.has_feature(db, alice, "chat") is True, "a feature not on the premium list is free")
e = client.get("/entitlements/me", headers=HA).json()
check(e["premium"] is False and e["commissioner_mode"] is False and e["draft_grades"] is False, "entitlements before paying")

# ---- webhook: signatures ----------------------------------------------------------------------------------------------
now = int(time.time())
ev = sub_event("evt_1", "customer.subscription.created", now - 50, A, "incomplete")
check(send(ev, secret="whsec_wrong").status_code == 400, "wrong secret: 400")
check(send(ev, header="").status_code == 400, "no signature: 400")
check(send(ev, header="t=abc,v1=zz").status_code == 400, "malformed signature: 400")
check(send(ev, t=now - 3600).status_code == 400, "old signature (replay): 400")
check(db.query(BillingEvent).count() == 0, "rejected events change nothing")

# ---- webhook: the normal order ----------------------------------------------------------------------------------------
check(send(ev).json()["result"] == "updated", "subscription.created stored")
done = {"id": "evt_2", "type": "checkout.session.completed", "created": now - 40, "livemode": False,
        "data": {"object": {"id": "cs_test_1", "mode": "subscription", "client_reference_id": str(A),
                            "customer": "cus_test_1", "subscription": "sub_test_1", "payment_status": "paid",
                            "metadata": {"user_id": str(A)}}}}
check(send(done).json()["result"] == "linked", "checkout.session.completed links the customer")
db.expire_all()
row = db.query(UserSubscription).filter_by(user_id=A).one()
check(row.status == "active" and row.stripe_customer_id == "cus_test_1", "paid checkout: premium on")
check(send(done).json()["result"] == "duplicate", "same event twice: applied once")
upd = sub_event("evt_3", "customer.subscription.updated", now - 30, A, "active", interval="year")
check(send(upd).json()["result"] == "updated", "subscription.updated stored")
db.expire_all()
row = db.query(UserSubscription).filter_by(user_id=A).one()
check(row.plan_interval == "year" and row.current_period_end is not None, "plan and renewal date kept")
check(entitlements.has_feature(db, db.get(User, A), entitlements.COMMISSIONER_MODE), "subscriber: commissioner mode on")
check(entitlements.has_feature(db, db.get(User, A), entitlements.DRAFT_GRADES), "subscriber: draft grades on")
check(not entitlements.has_feature(db, db.get(User, B), entitlements.COMMISSIONER_MODE), "other users unaffected")
me = client.get("/billing/me", headers=HA).json()
check(me["premium"] is True and me["status"] == "active" and me["can_manage"] is True, "billing/me reports premium")
check(client.get("/billing/plans", headers=HA).json()["me"]["premium"] is True, "pricing page sees premium")
r = client.post("/billing/checkout", json={"interval": "month"}, headers=HA)
check(r.status_code == 409, "already premium: no second checkout")

# ---- out of order, cancel, unknown users ------------------------------------------------------------------------------
late = sub_event("evt_0", "customer.subscription.updated", now - 45, A, "incomplete")
check(send(late).json()["result"] == "stale", "an older event arriving late is ignored")
db.expire_all()
check(db.query(UserSubscription).filter_by(user_id=A).one().status == "active", "still premium after the late event")
nometa = sub_event("evt_4", "customer.subscription.updated", now - 20, None, "past_due")
check(send(nometa).json()["result"] == "updated", "found by subscription id without metadata")
db.expire_all()
check(billing.is_premium(db, A), "past_due keeps premium while Stripe retries")
gone = sub_event("evt_5", "customer.subscription.deleted", now - 10, A, "canceled")
check(send(gone).json()["result"] == "updated", "subscription.deleted stored")
db.expire_all()
check(not billing.is_premium(db, A) and not entitlements.has_feature(db, db.get(User, A), entitlements.COMMISSIONER_MODE),
      "cancelled: premium off")
check(send(sub_event("evt_6", "customer.subscription.updated", now, 99999, "active", sub_id="sub_x", cust="cus_x")).json()["result"]
      == "unknown_user", "unknown user id: nothing created")
check(db.query(UserSubscription).count() == 1, "still one subscription row")
live = sub_event("evt_7", "customer.subscription.updated", now, B, "active", sub_id="sub_b", cust="cus_b")
live["livemode"] = True
check(send(live).json()["result"] == "ignored_live_event", "live event ignored in test mode")
check(not billing.is_premium(db, B), "live event granted nothing")
check(send({"id": "evt_8", "type": "invoice.paid", "created": now, "data": {"object": {}}}).json()["result"] == "ignored",
      "other event types are acknowledged and ignored")

# ---- re-subscribe reuses the customer; portal -------------------------------------------------------------------------
client.post("/billing/checkout", json={"interval": "month"}, headers=HA)
check(sent[-1][1].get("customer") == "cus_test_1" and "customer_email" not in sent[-1][1], "re-subscribe reuses the Stripe customer")
r = client.post("/billing/portal", headers=HA)
check(r.status_code == 200 and sent[-1][0] == "/billing_portal/sessions" and sent[-1][1]["customer"] == "cus_test_1",
      "portal for the subscriber's customer")
check(client.post("/billing/portal", headers=HB).status_code == 404, "no subscription: no portal")

# ---- draft grades gate over HTTP --------------------------------------------------------------------------------------
from fantasy_sports.api.routers import draft_grades as dg_router  # noqa: E402
from unittest.mock import patch  # noqa: E402

with patch.object(dg_router, "_require_nhl_league", lambda db, lid: None), \
     patch.object(dg_router.draft_grades, "draft_grades", lambda db, lid: {"available": False, "reason": "stub"}):
    check(client.get("/leagues/1/draft-grades").status_code == 402, "draft grades signed out: 402")
    check(client.get("/leagues/1/draft-grades", headers=HB).status_code == 402, "draft grades free user: 402")
    send(sub_event("evt_9", "customer.subscription.created", now + 5, B, "active", sub_id="sub_b", cust="cus_b"))
    check(client.get("/leagues/1/draft-grades", headers=HB).status_code == 200, "draft grades for a subscriber")
    os.environ["FSC_GRANT_ALL_PREMIUM"] = "1"
    check(client.get("/leagues/1/draft-grades").status_code == 200, "dev switch still grants everything")
    os.environ.pop("FSC_GRANT_ALL_PREMIUM")

# ---- signature helper, pure -------------------------------------------------------------------------------------------
body = b'{"x":1}'
billing.verify_signature(body, sign(body, t=1000), WHSEC, now=1100)
try:
    billing.verify_signature(body + b" ", sign(body, t=1000), WHSEC, now=1100)
    check(False, "tampered body must fail")
except billing.BillingError:
    check(True, "tampered body fails")
billing.verify_signature(body, f"t=1000,v1=deadbeef,v1={sign(body, t=1000).split('v1=')[1]}", WHSEC, now=1100)
check(True, "any one matching v1 signature passes (secret rotation)")

db.close()
engine.dispose()
if db_path.exists() and not os.environ.get("BILLING_KEEP_DB"):
    db_path.unlink()
print(f"All {checks} billing checks passed.")
