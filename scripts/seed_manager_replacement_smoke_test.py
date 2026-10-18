"""
DUE-06: replacing a franchise's manager keeps its prepaid credit, through the real HTTP API (FastAPI TestClient) on a
throwaway SQLite file (manager_replacement_smoke_test.db): the replacement inherits the dues and platform-fee credit and isn't
charged again, the old manager loses control, pending offers are cancelled, accepted trades run on, and the guards.

    .venv-api/Scripts/python.exe scripts/seed_manager_replacement_smoke_test.py
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("MANAGER_REPLACEMENT_SMOKE_DB", "manager_replacement_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import platform_fees
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, League, PlatformSeasonFee, Player, RealTeam, SeasonDues, Team, Trade, User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()

# This script reads through `db` between API calls; end its read transaction first so SQLite lets the API write.
_request = client.request


def _request_after_commit(*args, **kwargs):
    db.commit()
    return _request(*args, **kwargs)


client.request = _request_after_commit

nhl, _ = seed_hockey_reference_data(db)
real = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
db.add(real)
db.flush()
players = [Player(universe_id=nhl.id, real_team_id=real.id, full_name=f"Skater {i:03d}", position="C") for i in range(400)]
db.add_all(players)
db.flush()
db.add_all([DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id) for p in players])
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "a", "b", "c", "d", "new", "new2")}
db.add_all(users.values())
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


HDR = {n: login(u) for n, u in users.items()}
DUES = 5000
checks = 0


def check(cond, msg=""):
    global checks
    checks += 1
    assert cond, msg


resp = client.post("/leagues", json={"name": "Replace", "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"],
                                     "size": 4, "league_type": "PAID", "annual_dues_cents": DUES}, headers=HDR["commish"])
lid = resp.json()["id"]
assert client.patch(f"/leagues/{lid}/settings", json={"league_format": "dynasty"}, headers=HDR["commish"]).status_code == 200
teams = [Team(league_id=lid, name=f"Replace {n}", owner_user_id=users[n].id) for n in ("a", "b", "c", "d")]
db.add_all(teams)
db.commit()
a, b, c, d = [t.id for t in teams]
draft_id = client.post("/drafts/start", json={"league_id": lid}, headers=HDR["commish"]).json()["id"]
for _ in range(600):
    if client.post(f"/drafts/{draft_id}/autopick").status_code != 200:
        break
assert client.patch(f"/leagues/{lid}/settings", json={"platform_plan": "AD_FREE"}, headers=HDR["commish"]).status_code == 200
for t in (a, b):
    assert client.post(f"/leagues/{lid}/teams/{t}/dues", headers=HDR["commish"], json={
        "season_number": 2, "amount_cents": DUES, "status": "paid", "payment_reference": f"d-{t}"}).status_code == 200
    platform_fees.record_payment(db, db.get(League, lid), db.get(Team, t), 2, platform_fees.PLATFORM_FEE_CENTS, "paid", f"f-{t}", "t")
db.commit()


def pick_trade(me, them, who, mine, theirs):
    return client.post(f"/leagues/{lid}/trades", headers=HDR[who], json={
        "proposer_team_id": me, "counterparty_team_id": them,
        "send_picks": [{"season": s, "round": r, "original_team_id": o} for s, r, o in mine],
        "receive_picks": [{"season": s, "round": r, "original_team_id": o} for s, r, o in theirs]})


def replace(team, new_user, who="commish"):
    return client.post(f"/leagues/{lid}/teams/{team}/replace-manager", json={"user_id": users[new_user].id}, headers=HDR[who])


# a has an offer out to b (both prepaid for season 2).
offer = pick_trade(a, b, "a", [(2, 1, a)], [(2, 2, b)])
check(offer.status_code == 200 and offer.json()["status"] == "proposed", offer.text)
offer_id = offer.json()["id"]
dues_rows, fee_rows = db.query(SeasonDues).count(), db.query(PlatformSeasonFee).count()

# ---- Guards ------------------------------------------------------------------------------------------------------------
check(replace(a, "new", "a").status_code == 403, "only the commissioner")
check(replace(a, "new", "b").status_code == 403, "")
check(client.post(f"/leagues/{lid}/teams/{a}/replace-manager", json={"user_id": users["a"].id}, headers=HDR["commish"]).status_code == 400,
      "can't replace a manager with themselves")
check(replace(a, "b").status_code == 400, "the replacement can't already manage a team in the league")
check(client.post(f"/leagues/{lid}/teams/{a}/replace-manager", json={"user_id": 99999}, headers=HDR["commish"]).status_code == 400, "unknown user")
check(client.post(f"/leagues/{lid}/teams/99999/replace-manager", json={"user_id": users["new"].id}, headers=HDR["commish"]).status_code == 404, "")
db.get(Team, d).owner_user_id = None
db.get(Team, d).is_ai_owned = True
db.commit()
check(replace(d, "new").status_code == 400, "an AI team has no manager to replace")
db.get(Team, d).is_ai_owned = False
db.get(Team, d).owner_user_id = users["d"].id
db.commit()
db.expire_all()
check(db.get(Team, a).owner_user_id == users["a"].id, "refused calls changed nothing")

# ---- The replacement inherits the credit -------------------------------------------------------------------------------
resp = replace(a, "new")
check(resp.status_code == 200, resp.text)
out = resp.json()
check(out["new_user_id"] == users["new"].id and out["previous_user_id"] == users["a"].id, out)
check([(x["kind"], x["season_number"]) for x in out["credits"]] == [("league_dues", 2), ("platform_fee", 2)], "credit is listed: " + str(out))
check(out["cancelled_trade_ids"] == [offer_id], "the old manager's pending offer is cancelled")
db.expire_all()
check(db.get(Trade, offer_id).status == "cancelled", "")
check(db.query(SeasonDues).count() == dues_rows and db.query(PlatformSeasonFee).count() == fee_rows, "no payment record was touched")
check(all(r.status == "paid" for r in db.query(SeasonDues).filter_by(team_id=a)), "the credit still stands")

check(pick_trade(a, b, "a", [(2, 1, a)], [(2, 2, b)]).status_code == 403, "the old manager lost control")
dues_view = client.get(f"/leagues/{lid}/dues", headers=HDR["new"]).json()
check([r["team_id"] for r in dues_view] == [a], "the replacement sees the credit")
resp = pick_trade(a, b, "new", [(2, 1, a)], [(2, 2, b)])
check(resp.status_code == 200 and resp.json()["eligibility"]["payment_requirements"] == [],
      "the replacement trades on the prepaid season without paying again: " + resp.text)
check(client.post(f"/leagues/{lid}/teams/{a}/dues", headers=HDR["commish"], json={
    "season_number": 2, "amount_cents": DUES, "status": "paid", "payment_reference": "again"}).status_code == 400, "and isn't charged twice")
try:
    platform_fees.record_payment(db, db.get(League, lid), db.get(Team, a), 2, platform_fees.PLATFORM_FEE_CENTS, "paid", "again", "t")
    check(False, "fee charged twice")
except ValueError:
    db.rollback()
    check(True)

# ---- An accepted trade runs on through a replacement -------------------------------------------------------------------
tid = resp.json()["id"]
resp = client.post(f"/trades/{tid}/respond", json={"team_id": b, "accept": True}, headers=HDR["b"])
check(resp.status_code == 200 and resp.json()["status"] == "in_review", resp.text)
check(replace(b, "new2").status_code == 200, "")
db.expire_all()
check(db.get(Trade, tid).status == "in_review", "a trade already accepted isn't cancelled by a replacement")
check(client.post(f"/trades/{tid}/cancel", headers=HDR["b"]).status_code == 403, "the old manager can't cancel it any more")
check(client.post(f"/trades/{tid}/cancel", headers=HDR["new2"]).json()["status"] == "cancelled", "the replacement can")

print(f"manager replacement smoke test: {checks} checks passed")
