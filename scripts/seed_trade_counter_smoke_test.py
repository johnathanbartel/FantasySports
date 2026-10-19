"""
Trade counter offers, through the real HTTP API (FastAPI TestClient) on a throwaway SQLite file (trade_counter_smoke_test.db):
the receiving manager counters a direct offer, the original closes as 'countered' and points at the counter, only the receiving
team can counter, the counter is checked like any proposal (a refused counter leaves the original open), a counter that repeats
the offer is refused, counters can be countered back, accepting a counter swaps exactly what the counter lists, and an offer
involving an AI team (put to the league) can't be countered.

    .venv-api/Scripts/python.exe scripts/seed_trade_counter_smoke_test.py
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("TRADE_COUNTER_SMOKE_DB", "trade_counter_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, Player, RealTeam, RosterEntry, Team, Trade, User,
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
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "a", "b", "c", "d")}
db.add_all(users.values())
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


HDR = {n: login(u) for n, u in users.items()}
checks = 0


def check(cond, msg=""):
    global checks
    checks += 1
    assert cond, msg


def make_league(name):
    """Four person-managed teams a-d (drafted), then an AI team e added after the draft."""
    body = {"name": name, "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"], "size": 4, "league_type": "FREE"}
    lid = client.post("/leagues", json=body, headers=HDR["commish"]).json()["id"]
    resp = client.patch(f"/leagues/{lid}/settings", json={"league_format": "dynasty"}, headers=HDR["commish"])
    assert resp.status_code == 200, resp.text
    teams = [Team(league_id=lid, name=f"{name} {n}", owner_user_id=users[n].id) for n in ("a", "b", "c", "d")]
    db.add_all(teams)
    db.commit()
    draft_id = client.post("/drafts/start", json={"league_id": lid}, headers=HDR["commish"]).json()["id"]
    for _ in range(600):
        if client.post(f"/drafts/{draft_id}/autopick").status_code != 200:
            break
    e = Team(league_id=lid, name=f"{name} e", owner_user_id=None, is_ai_owned=True)
    db.add(e)
    db.commit()
    return lid, [t.id for t in teams] + [e.id]


def roster(tid):
    return sorted(r.draftable_asset_id for r in db.query(RosterEntry).filter_by(team_id=tid))


def owner_of(aid, lid):
    row = (db.query(RosterEntry).join(Team, Team.id == RosterEntry.team_id)
           .filter(Team.league_id == lid, RosterEntry.draftable_asset_id == aid).first())
    return row.team_id if row else None


def propose(lid, me, them, who, send, receive):
    resp = client.post(f"/leagues/{lid}/trades", headers=HDR[who], json={
        "proposer_team_id": me, "counterparty_team_id": them, "send_asset_ids": send, "receive_asset_ids": receive})
    assert resp.status_code == 200, resp.text
    return resp.json()


def counter(tid, team, who, send, receive):
    return client.post(f"/trades/{tid}/counter", headers=HDR[who], json={
        "team_id": team, "send_asset_ids": send, "receive_asset_ids": receive})


def respond(tid, team, who, accept=True):
    return client.post(f"/trades/{tid}/respond", json={"team_id": team, "accept": accept}, headers=HDR[who])


def get(tid):
    resp = client.get(f"/trades/{tid}", headers=HDR["a"])
    assert resp.status_code == 200, resp.text
    return resp.json()


lid, (a, b, c, d, e) = make_league("Counter")
# no review window, so an accepted trade executes at once and the rosters can be checked straight away
check(client.patch(f"/leagues/{lid}/settings", json={"trade_review_hours": 0}, headers=HDR["commish"]).status_code == 200,
      "review window off")
ra, rb = roster(a), roster(b)

# ---- a offers b one for one; b counters asking for more ----------------------------------------------------------------
offer = propose(lid, a, b, "a", [ra[0]], [rb[0]])
check(offer["status"] == "proposed", offer)

check(counter(offer["id"], a, "a", [ra[0]], [rb[0], rb[1]]).status_code == 400, "the proposer can't counter his own offer")
check(counter(offer["id"], b, "c", [rb[0]], [ra[0]]).status_code in (400, 403), "a stranger can't counter for b")
check(counter(offer["id"], b, "a", [rb[0]], [ra[0]]).status_code in (400, 403), "a can't counter as team b")
check(counter(offer["id"], b, "b", [rb[0]], [ra[0]]).status_code == 400, "a counter that repeats the offer is refused")
resp = counter(offer["id"], b, "b", [rb[0]], [999999])
check(resp.status_code == 400, "a player the proposer doesn't have is refused: " + resp.text)
resp = counter(offer["id"], b, "b", [], [ra[0]])
check(resp.status_code == 400, "each side has to give something: " + resp.text)
check(get(offer["id"])["status"] == "proposed", "refused counters leave the offer open")

resp = counter(offer["id"], b, "b", [rb[0]], [ra[1]])
check(resp.status_code == 200, resp.text)
ctr = resp.json()
check(ctr["status"] == "proposed" and ctr["counter_of_trade_id"] == offer["id"], ctr)
check(ctr["proposer_team_id"] == b and ctr["counterparty_team_id"] == a, "the counter goes back to the original proposer")
check([x["draftable_asset_id"] for x in ctr["send_assets"]] == [rb[0]]
      and [x["draftable_asset_id"] for x in ctr["receive_assets"]] == [ra[1]], "sides are b's")
old = get(offer["id"])
check(old["status"] == "countered" and old["countered_by_trade_id"] == ctr["id"] and old["resolved_at"] is not None, old)
check(owner_of(ra[0], lid) == a and owner_of(rb[0], lid) == b, "countering moves nothing")
check(respond(offer["id"], b, "b").status_code == 400, "the countered offer can no longer be accepted")
check(counter(offer["id"], b, "b", [rb[0]], [ra[2]]).status_code == 400, "or countered twice")
check(client.post(f"/trades/{offer['id']}/cancel", json={}, headers=HDR["a"]).status_code == 400, "or cancelled")
listed = client.get(f"/leagues/{lid}/trades", headers=HDR["a"]).json()
check({t["id"] for t in listed} >= {offer["id"], ctr["id"]}, "both show in the league's trade list")

# ---- a counters the counter back, b accepts that one ---------------------------------------------------------------------
check(counter(ctr["id"], b, "b", [rb[0]], [ra[0]]).status_code == 400, "now b is the proposer and can't counter")
resp = counter(ctr["id"], a, "a", [ra[1]], [rb[1]])
check(resp.status_code == 200, resp.text)
ctr2 = resp.json()
check(ctr2["counter_of_trade_id"] == ctr["id"] and ctr2["proposer_team_id"] == a and ctr2["status"] == "proposed", ctr2)
check(get(ctr["id"])["status"] == "countered", "the first counter closes as countered")

resp = respond(ctr2["id"], b, "b")
check(resp.status_code == 200 and resp.json()["status"] == "accepted", resp.text)
check(owner_of(ra[1], lid) == b and owner_of(rb[1], lid) == a, "accepting the last counter swaps exactly that")
check(owner_of(ra[0], lid) == a and owner_of(rb[0], lid) == b and owner_of(ra[2], lid) == a, "nothing else moved")

# ---- declining a counter ends it; the original stays closed -----------------------------------------------------------------
ra, rb = roster(a), roster(b)
offer2 = propose(lid, a, b, "a", [ra[0]], [rb[0]])
ctr3 = counter(offer2["id"], b, "b", [rb[1]], [ra[0]]).json()
check(respond(ctr3["id"], a, "a", accept=False).json()["status"] == "declined", "a declines the counter")
check(get(offer2["id"])["status"] == "countered", "the original stays countered")

# ---- the proposer can cancel his pending counter ---------------------------------------------------------------------------
offer3 = propose(lid, a, b, "a", [ra[0]], [rb[0]])
ctr4 = counter(offer3["id"], b, "b", [rb[1]], [ra[0]]).json()
check(client.post(f"/trades/{ctr4['id']}/cancel", json={}, headers=HDR["b"]).json()["status"] == "cancelled", "b withdraws his counter")

# ---- a counter is checked like any proposal: roster limits, ownership at counter time ----------------------------------------
ra, rb = roster(a), roster(b)
offer4 = propose(lid, a, b, "a", [ra[0]], [rb[0]])
resp = counter(offer4["id"], b, "b", [rb[0]], [ra[0], ra[1]])      # rosters are full, so one-for-two busts team b
check(resp.status_code == 400, "a counter that busts the roster limit is refused: " + resp.text)
check(get(offer4["id"])["status"] == "proposed", "and the offer stays open")
check(db.query(Trade).filter(Trade.counter_of_trade_id == offer4["id"]).count() == 0, "with no counter row left behind")

# ---- AI-involved offers are put to the league and can't be countered ---------------------------------------------------------
db.query(RosterEntry).filter_by(team_id=d, draftable_asset_id=roster(d)[0]).update({"team_id": e})   # the AI team joined after the draft: give it a player
db.commit()
ra, re_ = roster(a), roster(e)
ai_offer = propose(lid, a, e, "a", [ra[0]], [re_[0]])
check(ai_offer["status"] in ("voting", "accepted", "rejected"), ai_offer)
check(counter(ai_offer["id"], e, "a", [re_[0]], [ra[0]]).status_code in (400, 403), "no counters on an AI trade")

print(f"trade counter offers: {checks} checks passed")
