"""
FS-036: platform plans and the per-season platform deposit, through the real HTTP API (FastAPI TestClient) on a throwaway
SQLite file (platform_fee_smoke_test.db): AD_FREE deposit gate (once per team per season), PAID + AD_FREE needing both dues and
fee, AI senders owing nothing, the refund lock on payments that backed an executed trade, franchise continuity, the FREE-league
pick lock, and ads_enabled.

    .venv-api/Scripts/python.exe scripts/seed_platform_fee_smoke_test.py
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("PLATFORM_FEE_SMOKE_DB", "platform_fee_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import dues, pick_trading, platform_fees, trade_eligibility
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    Draft, DraftableAsset, League, PlatformSeasonFee, Player, RealTeam, SeasonDues, Team, Trade, User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()

nhl, _ = seed_hockey_reference_data(db)
real = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
db.add(real)
db.flush()
players = [Player(universe_id=nhl.id, real_team_id=real.id, full_name=f"Skater {i:03d}", position="C") for i in range(400)]
db.add_all(players)
db.flush()
db.add_all([DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id) for p in players])
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "a", "b", "c", "d", "stranger")}
db.add_all(users.values())
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


HDR = {n: login(u) for n, u in users.items()}
DUES = 5000
FEE = platform_fees.PLATFORM_FEE_CENTS
checks = 0


def check(cond, msg=""):
    global checks
    checks += 1
    assert cond, msg


def make_league(name, league_type="PAID", plan="AD_SUPPORTED"):
    body = {"name": name, "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"], "size": 4,
            "league_type": league_type}
    if league_type == "PAID":
        body["annual_dues_cents"] = DUES
    resp = client.post("/leagues", json=body, headers=HDR["commish"])
    assert resp.status_code == 200, resp.text
    lid = resp.json()["id"]
    assert client.patch(f"/leagues/{lid}/settings", json={"league_format": "dynasty", "trade_review_hours": 0},
                        headers=HDR["commish"]).status_code == 200
    teams = [Team(league_id=lid, name=f"{name} {n}", owner_user_id=users[n].id) for n in ("a", "b", "c", "d")]
    db.add_all(teams)
    db.commit()
    resp = client.post("/drafts/start", json={"league_id": lid}, headers=HDR["commish"])
    assert resp.status_code == 200, resp.text
    draft_id = resp.json()["id"]
    for _ in range(600):
        if client.post(f"/drafts/{draft_id}/autopick").status_code != 200:
            break
    if plan != "AD_SUPPORTED":
        resp = client.patch(f"/leagues/{lid}/settings", json={"platform_plan": plan}, headers=HDR["commish"])
        assert resp.status_code == 200, resp.text
    return lid, [t.id for t in teams]


def propose(lid, me, them, who, send_picks=(), receive_picks=()):
    return client.post(f"/leagues/{lid}/trades", headers=HDR[who], json={
        "proposer_team_id": me, "counterparty_team_id": them,
        "send_picks": [{"season": s, "round": r, "original_team_id": o} for s, r, o in send_picks],
        "receive_picks": [{"season": s, "round": r, "original_team_id": o} for s, r, o in receive_picks]})


def preview(lid, me, them, who, send_picks=(), receive_picks=()):
    return client.post(f"/leagues/{lid}/trades/preview", headers=HDR[who], json={
        "proposer_team_id": me, "counterparty_team_id": them,
        "send_picks": [{"season": s, "round": r, "original_team_id": o} for s, r, o in send_picks],
        "receive_picks": [{"season": s, "round": r, "original_team_id": o} for s, r, o in receive_picks]}).json()


def pay_dues(lid, tid, season, ref=None):
    ref = ref or f"dues-{lid}-{tid}-{season}"
    return client.post(f"/leagues/{lid}/teams/{tid}/dues", headers=HDR["commish"], json={
        "season_number": season, "amount_cents": DUES, "status": "paid", "payment_reference": ref})


def pay_fee(lid, tid, season, status="paid", ref=None, amount=FEE):
    league, team = db.get(League, lid), db.get(Team, tid)
    row = platform_fees.record_payment(db, league, team, season, amount, status, ref or f"fee-{lid}-{tid}-{season}-{status}", "test")
    db.commit()
    return row


def kinds(res):
    return sorted((p["kind"], p["team_id"], p["season_number"]) for p in res["payment_requirements"])


# ---- Plans and the fee snapshot --------------------------------------------------------------------------------------
lid, (a, b, c, d) = make_league("Plans")
league = db.get(League, lid)
check(league.platform_plan == "AD_SUPPORTED" and league.platform_fee_cents is None, "default plan is AD_SUPPORTED")
check(client.patch(f"/leagues/{lid}/settings", json={"platform_plan": "AD_FREE"}, headers=HDR["a"]).status_code == 403, "commissioner only")
draft = db.query(Draft).filter_by(league_id=lid).first()
draft.status = "in_progress"
db.commit()
check(client.patch(f"/leagues/{lid}/settings", json={"platform_plan": "AD_FREE"}, headers=HDR["commish"]).status_code == 409,
      "no plan change while a draft is in progress")
draft.status = "complete"
db.commit()
resp = client.patch(f"/leagues/{lid}/settings", json={"platform_plan": "AD_FREE"}, headers=HDR["commish"])
check(resp.status_code == 200 and resp.json()["platform_plan"] == "AD_FREE" and resp.json()["platform_fee_cents"] == FEE, resp.text)
with patch.object(platform_fees, "PLATFORM_FEE_CENTS", 9999):
    check(db.get(League, lid).platform_fee_cents == FEE, "a later constant change doesn't alter a running league")
check(client.patch(f"/leagues/{lid}/settings", json={"platform_plan": "BOGUS"}, headers=HDR["commish"]).status_code == 422, "bad plan")
check(client.patch(f"/leagues/{lid}/settings", json={"trade_review_hours": 99}, headers=HDR["commish"]).status_code == 422, "0-72 hours")
first_fee = pay_fee(lid, a, 2)
check(first_fee.status == "paid", "fee recorded")
check(pay_fee(lid, a, 2).id == first_fee.id, "same reference is idempotent")
try:
    pay_fee(lid, a, 2, ref="second")
    check(False, "a secured season is collected twice")
except ValueError:
    check(True)
db.expire_all()
check(client.patch(f"/leagues/{lid}/settings", json={"platform_plan": "AD_SUPPORTED"}, headers=HDR["commish"]).status_code == 200, "")
check(db.query(PlatformSeasonFee).filter_by(league_id=lid).count() == 1, "switching back keeps every record")
check(not platform_fees.is_secured(db, db.get(League, lid), a, 2), "ad-supported leagues have no secured fee")
client.patch(f"/leagues/{lid}/settings", json={"platform_plan": "AD_FREE"}, headers=HDR["commish"])
db.expire_all()
check(platform_fees.is_secured(db, db.get(League, lid), a, 2), "and the credit is back when ad-free again")
try:
    platform_fees.record_payment(db, db.get(League, lid), db.get(Team, a), 2, FEE, "bogus", "x", "t")
    check(False, "bad status")
except ValueError:
    check(True)

# ---- (a)(b) PAID + AD_FREE needs both dues and the fee, once per team per season ---------------------------------------
lid, (a, b, c, d) = make_league("Both", "PAID", "AD_FREE")
mine, theirs = [(2, 1, a)], [(2, 2, b)]
res = preview(lid, a, b, "a", mine, theirs)
check(res["status"] == "AWAITING_PAYMENT", res)
check(kinds(res) == sorted([("league_dues", a, 2), ("league_dues", b, 2), ("platform_fee", a, 2), ("platform_fee", b, 2)]), res)
check(all(p["amount_cents"] == (DUES if p["kind"] == "league_dues" else FEE) for p in res["payment_requirements"]), res)
check(any("platform deposit" in e and "once per season" in e for e in res["explanations"]), res["explanations"])
check(propose(lid, a, b, "a", mine, theirs).status_code == 400, "can't submit unpaid")
check(pay_dues(lid, a, 2).status_code == 200 and pay_dues(lid, b, 2).status_code == 200, "")
res = preview(lid, a, b, "a", mine, theirs)
check(kinds(res) == sorted([("platform_fee", a, 2), ("platform_fee", b, 2)]), "dues alone aren't enough: " + str(res))
pay_fee(lid, a, 2, "pending", "p1")
pay_fee(lid, a, 2, "failed", "f1")
check(preview(lid, a, b, "a", mine, theirs)["status"] == "AWAITING_PAYMENT", "pending/failed fees don't count")
pay_fee(lid, a, 2)
res = preview(lid, a, b, "a", mine, theirs)
check(kinds(res) == [("platform_fee", b, 2)], "only b still owes: " + str(res))
pay_fee(lid, b, 2)
resp = propose(lid, a, b, "a", mine, theirs)
check(resp.status_code == 200 and resp.json()["status"] == "proposed", resp.text)
tid1 = resp.json()["id"]
resp = client.post(f"/trades/{tid1}/respond", json={"team_id": b, "accept": True}, headers=HDR["b"])
check(resp.status_code == 200 and resp.json()["status"] == "accepted", resp.text)
# (a) a second season-2 pick trade asks for nothing
res = preview(lid, a, b, "a", [(2, 2, a)], [(2, 1, b)])
check(res["status"] == "ELIGIBLE_FOR_REVIEW" and res["payment_requirements"] == [], "charged once per season: " + str(res))
# ... and a season-3 pick asks for season 3, for both gates
res = preview(lid, a, b, "a", [(3, 1, a)], [(2, 1, b)])
check(kinds(res) == sorted([("league_dues", a, 3), ("platform_fee", a, 3)]), "season 3 is a new season: " + str(res))
# only the SENDER pays: b sends nothing in season 3
check(all(p["team_id"] == a for p in res["payment_requirements"]), res)

# ---- (d) the refund lock -----------------------------------------------------------------------------------------------
db.expire_all()
fee_a = db.query(PlatformSeasonFee).filter_by(team_id=a, season_number=2, status="paid").one()
fee_b = db.query(PlatformSeasonFee).filter_by(team_id=b, season_number=2, status="paid").one()
dues_a = db.query(SeasonDues).filter_by(team_id=a, season_number=2, status="paid").one()
check(fee_a.committed_by_trade_id == tid1 and fee_b.committed_by_trade_id == tid1 and dues_a.committed_by_trade_id == tid1,
      "every record that backed the executed trade is committed to it")
try:
    platform_fees.set_status(db, db.get(League, lid), fee_a, "refunded", "t")
    check(False, "refund of a committed fee")
except ValueError as e:
    check(str(tid1) in str(e), str(e))
resp = client.post(f"/leagues/{lid}/dues/{dues_a.id}/status", json={"status": "refunded"}, headers=HDR["commish"])
check(resp.status_code == 400 and str(tid1) in resp.json()["detail"], "dues can't be refunded either: " + resp.text)
db.expire_all()
check(db.get(SeasonDues, dues_a.id).status == "paid" and db.get(PlatformSeasonFee, fee_a.id).status == "paid", "nothing changed")
# a chargeback is still possible: the executed trade stays, the next pick trade for that season is blocked again
platform_fees.set_status(db, db.get(League, lid), db.get(PlatformSeasonFee, fee_a.id), "disputed", "t")
db.commit()
check(db.get(Trade, tid1).status == "accepted" and pick_trading.current_owner(db, lid, 2, 1, a) == b, "executed trades are never reversed")
res = preview(lid, a, b, "a", [(2, 2, a)], [(2, 1, b)])
check(kinds(res) == [("platform_fee", a, 2)], "the disputed season is unsecured again: " + str(res))
check(propose(lid, a, b, "a", [(2, 2, a)], [(2, 1, b)]).status_code == 400, "")
# a record that is not committed can still be refunded (the first trade that used a record keeps it)
pay_fee(lid, c, 3)
row_c = db.query(PlatformSeasonFee).filter_by(team_id=c, season_number=3).one()
check(platform_fees.set_status(db, db.get(League, lid), row_c, "refunded", "t").status == "refunded", "uncommitted fees can be refunded")
db.commit()
# paying the disputed season again secures it again
pay_fee(lid, a, 2, "paid", "re-pay")
check(preview(lid, a, b, "a", [(2, 2, a)], [(2, 1, b)])["status"] == "ELIGIBLE_FOR_REVIEW", "a fresh payment re-secures the season")

# ---- (b) PAID + AD_SUPPORTED needs only dues ---------------------------------------------------------------------------
lid, (a, b, c, d) = make_league("DuesOnly", "PAID")
res = preview(lid, a, b, "a", [(2, 1, a)], [(2, 2, b)])
check(kinds(res) == sorted([("league_dues", a, 2), ("league_dues", b, 2)]), "ad-supported: dues only: " + str(res))
check(pay_dues(lid, a, 2).status_code == 200 and pay_dues(lid, b, 2).status_code == 200, "")
check(preview(lid, a, b, "a", [(2, 1, a)], [(2, 2, b)])["status"] == "ELIGIBLE_FOR_REVIEW", "no platform fee asked")

# ---- (c) an AI-owned sender owes nothing -------------------------------------------------------------------------------
lid, (a, b, c, d) = make_league("AiSender", "PAID", "AD_FREE")
db.get(Team, c).owner_user_id = None
db.get(Team, c).is_ai_owned = True
db.commit()
league = db.get(League, lid)
res = trade_eligibility.evaluate_proposal(db, league, db.get(Team, c), db.get(Team, d), [], [], [(2, 1, c)], [(2, 2, d)]).as_dict()
check(kinds(res) == sorted([("league_dues", d, 2), ("platform_fee", d, 2)]), "only the person-managed team pays: " + str(res))
try:
    platform_fees.record_payment(db, league, db.get(Team, c), 2, FEE, "paid", "ai", "t")
    check(False, "AI team fee")
except ValueError:
    check(True)
db.get(Team, c).is_ai_owned = False
db.get(Team, c).owner_user_id = users["c"].id
db.commit()

# ---- (e) a replacement manager keeps the credit ------------------------------------------------------------------------
lid, (a, b, c, d) = make_league("Continuity", "PAID", "AD_FREE")
for t in (a, b):
    check(pay_dues(lid, t, 2).status_code == 200, "")
    pay_fee(lid, t, 2)
db.get(Team, a).owner_user_id = users["stranger"].id
db.commit()
resp = propose(lid, a, b, "stranger", [(2, 1, a)], [(2, 2, b)])
check(resp.status_code == 200, "the replacement trades on the prepaid season: " + resp.text)
try:
    pay_fee(lid, a, 2, ref="dup")
    check(False, "charged again")
except ValueError:
    check(True)

# ---- (f) FREE leagues cannot trade picks, whatever the plan ------------------------------------------------------------
lid, (a, b, c, d) = make_league("FreePicks", "FREE")
res = preview(lid, a, b, "a", [(2, 1, a)], [(2, 2, b)])
check(res["status"] == "BLOCKED" and res["blocking_codes"] == ["FREE_LEAGUE_PICK_TRADING_LOCKED"], res)
resp = client.patch(f"/leagues/{lid}/settings", json={"platform_plan": "AD_FREE"}, headers=HDR["commish"])
check(resp.status_code == 200, resp.text)
res = preview(lid, a, b, "a", [(2, 1, a)], [(2, 2, b)])
check(res["status"] == "BLOCKED" and "FREE_LEAGUE_PICK_TRADING_LOCKED" in res["blocking_codes"], "ad-free doesn't unlock picks: " + str(res))
check(propose(lid, a, b, "a", [(2, 1, a)], [(2, 2, b)]).status_code == 400, "")

# ---- AD_FREE with no fee snapshot is a configuration error --------------------------------------------------------------
lid, (a, b, c, d) = make_league("NoFee", "PAID", "AD_FREE")
db.get(League, lid).platform_fee_cents = None
db.commit()
res = preview(lid, a, b, "a", [(2, 1, a)], [(2, 2, b)])
check(res["status"] == "BLOCKED" and "PLATFORM_FEE_NOT_CONFIGURED" in res["blocking_codes"], res)

# ---- (g) ads_enabled ---------------------------------------------------------------------------------------------------
lid, (a, b, c, d) = make_league("Ads", "PAID", "AD_FREE")
league = db.get(League, lid)
cur = league.season_number
check(platform_fees.ads_enabled(db, league, db.get(Team, a)), "unpaid: ads on")
pay_fee(lid, a, cur + 1)
check(platform_fees.ads_enabled(db, league, db.get(Team, a)), "a deposit for a future season doesn't turn ads off yet")
pay_fee(lid, a, cur)
check(not platform_fees.ads_enabled(db, league, db.get(Team, a)), "current season secured: ads off")
check(platform_fees.ads_enabled(db, league, db.get(Team, b)), "another team is unaffected")
teams = {t["id"]: t for t in client.get(f"/leagues/{lid}/teams", headers=HDR["commish"]).json()}
check(teams[a]["ads_enabled"] is False and teams[b]["ads_enabled"] is True, "exposed on the team payload")
db.get(Team, b).owner_user_id = None
db.get(Team, b).is_ai_owned = True
db.commit()
check(platform_fees.ads_enabled(db, league, db.get(Team, b)), "AI teams: True")
db.get(Team, b).is_ai_owned = False
db.get(Team, b).owner_user_id = users["b"].id
db.commit()
client.patch(f"/leagues/{lid}/settings", json={"platform_plan": "AD_SUPPORTED"}, headers=HDR["commish"])
check(platform_fees.ads_enabled(db, db.get(League, lid), db.get(Team, a)), "ad-supported: ads on")

print(f"platform fee smoke test: {checks} checks passed")
