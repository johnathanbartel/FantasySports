"""
Acquisition types on a throwaway SQLite file (acquisition_types_smoke_test.db): no waivers, waivers, full waivers and
budget (bids). A league is created through the HTTP API with each type; the waiver engine is then driven directly with
explicit clocks. Checks the holds, the AI's roster fill ignoring a full-waivers hold, bids (highest wins, ties go to the
waiver order, non-refundable, over-budget refused), a paid league's budget being its dues, and the season reset.

    .venv-api/Scripts/python.exe scripts/seed_acquisition_types_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("ACQUISITION_SMOKE_DB", "acquisition_types_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import free_agency, waivers
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    Draft, DraftableAsset, FreeAgencyTransaction, League, Player, RealTeam, RosterEntry, Team, User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

checks = 0


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


def raises(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except ValueError as e:
        return str(e)
    return None


Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()
now = datetime.datetime(2026, 10, 14, 18, 0)          # a Wednesday

nhl, _ = seed_hockey_reference_data(db)
bos = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
db.add(bos)
db.flush()
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "a", "b")}
db.add_all(users.values())
db.commit()
resp = client.post("/sessions", json={"user_id": users["commish"].id})
HDR = {"Authorization": f"Bearer {resp.json()['token']}"}


def new_league(kind, **extra):
    """A league with two person teams (A, B) and one AI team, a finished draft and four free players. Returns (league, A, B, AI, assets)."""
    db.commit()                                         # SQLite: free the write lock before the API writes
    resp = client.post("/leagues", json={"name": f"L-{kind}", "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"],
                                         "size": 4, "acquisition_type": kind, **extra}, headers=HDR)
    assert resp.status_code == 200, resp.text
    lg = db.get(League, resp.json()["id"])
    check(lg.acquisition_type == kind and resp.json()["acquisition_type"] == kind, f"{kind}: type saved and returned")
    teams = [Team(league_id=lg.id, name=n, owner_user_id=u, is_ai_owned=u is None)
             for n, u in (("Alpha", users["a"].id), ("Bravo", users["b"].id), ("Robo", None))]
    db.add_all(teams)
    players = [Player(universe_id=nhl.id, real_team_id=bos.id, full_name=f"{kind} P{i}", position="C") for i in range(5)]
    db.add_all(players)
    db.flush()
    assets = []
    for p in players:
        a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
        db.add(a)
        db.flush()
        assets.append(a.id)
    db.add(Draft(league_id=lg.id, status="complete", completed_at=now - datetime.timedelta(days=5)))
    db.commit()
    return lg, teams[0], teams[1], teams[2], assets


def drop_one(team, asset, at):
    db.add(RosterEntry(team_id=team.id, draftable_asset_id=asset, acquired_via="draft"))
    db.flush()
    free_agency.drop_player(db, team, asset, "user", at)


# --- no waivers: a drop is a free agent at once ---------------------------------------------------------------------------------
lg, A, B, AI, p = new_league("no_waivers")
drop_one(A, p[0], now)
check(waivers.waiver_until(db, lg.id, now) == {}, "no waivers: nothing is held")
free_agency.add_player(db, B, p[0], "user", now)
check(any(r.team_id == B.id and r.draftable_asset_id == p[0] for r in db.query(RosterEntry)), "no waivers: signed straight away")

# --- waivers: held 24 hours, then the claim signs him ---------------------------------------------------------------------------
lg, A, B, AI, p = new_league("waivers")
drop_one(A, p[0], now)
until = waivers.waiver_until(db, lg.id, now)
check(set(until) == {p[0]} and until[p[0]] == now + datetime.timedelta(hours=24), "waivers: only the dropped player, for 24 hours")
check(raises(free_agency.add_player, db, B, p[0], "user", now), "waivers: can't sign outright")
claim = waivers.place_claim(db, B, p[0], 999, now)
check(claim.bid == 0, "waivers: a bid is ignored")
waivers.process(db, lg.id, now + datetime.timedelta(hours=25))
check(claim.status == "won", "waivers: claim won after the hold")

# --- full waivers: everyone free is always on waivers ---------------------------------------------------------------------------
lg, A, B, AI, p = new_league("full_waivers")
held = waivers.waiver_until(db, lg.id, now)
check(set(p) <= set(held), "full waivers: every free player is held")
check(waivers.waiver_until(db, lg.id, now, blanket=False) == {}, "full waivers: the AI fill sees no blanket hold")
check(raises(free_agency.add_player, db, A, p[1], "user", now), "full waivers: can't sign outright")
end = held[p[1]]
check(end == waivers.next_reset(now), "full waivers: no processing days -> the weekly reset")
c = waivers.place_claim(db, A, p[1], 0, now)
waivers.process(db, lg.id, end - datetime.timedelta(minutes=1))
check(c.status == "pending", "full waivers: not before the processing time")
waivers.process(db, lg.id, end)
check(c.status == "won", "full waivers: claim won at the processing time")
check(p[1] not in waivers.waiver_until(db, lg.id, end), "full waivers: a rostered player is no longer held")
lg.waiver_processing_days = [2]                                    # Wednesdays
db.flush()
mid = waivers.next_processing(lg, now)
check(mid.weekday() == 2 or mid.weekday() == 3, "full waivers: processing day respected (midnight Pacific is the next UTC day)")
check(mid > now and mid - now <= datetime.timedelta(days=7), "full waivers: next processing day is within a week")
board = {b.draftable_asset_id for b in free_agency.free_agent_board(db, lg.id)}
check(set(p[2:]) <= board and p[1] not in board, "full waivers: the AI's board still lists free players")

# --- budget: highest bid wins, non-refundable ----------------------------------------------------------------------------------
lg, A, B, AI, p = new_league("budget")
check(waivers.budget_total(lg) == 100, "budget: free league default is 100")
drop_one(AI, p[0], now)
drop_one(AI, p[1], now)
check(raises(waivers.place_claim, db, A, p[0], 101, now) is not None, "budget: over the budget is refused")
check(raises(waivers.place_claim, db, A, p[0], -1, now) is not None, "budget: negative bid refused")
ca = waivers.place_claim(db, A, p[0], 30, now)
cb = waivers.place_claim(db, B, p[0], 50, now)
ca2 = waivers.place_claim(db, A, p[1], 40, now)
cb2 = waivers.place_claim(db, B, p[1], 40, now)
waivers.process(db, lg.id, now + datetime.timedelta(hours=25))
check(cb.status == "won" and ca.status == "lost", "budget: the higher bid wins")
paid = db.query(FreeAgencyTransaction).filter_by(team_id=B.id, kind="add", draftable_asset_id=p[0]).one()
check(paid.price == 50, "budget: winner pays his bid, not the runner-up's")
later = now + datetime.timedelta(hours=26)
check({ca2.status, cb2.status} == {"won", "lost"}, "budget: a tie has one winner (decided by the waiver order)")
first = A if ca2.status == "won" else B
check(waivers.budget_left(db, B) == (10 if first is B else 50) and waivers.budget_left(db, A) == (60 if first is A else 100),
      "budget: the winner of a tie pays his bid, the loser nothing (bids are not refunded because they are never charged)")
free_agency.add_player(db, A, p[3], "user", later)               # a never-rostered free agent is signed for nothing
check(waivers.budget_left(db, A) == (60 if first is A else 100), "budget: a direct signing costs nothing")
db.query(FreeAgencyTransaction).filter_by(league_id=lg.id).delete()          # a season rollover clears the live transactions
db.flush()
check(waivers.budget_left(db, A) == 100 and waivers.budget_left(db, B) == 100, "budget: starts over each season")

# --- budget in a paid league is the dues; direct edit through the API ------------------------------------------------------------
lg, A, B, AI, p = new_league("budget", league_type="PAID", annual_dues_cents=7500)
check(waivers.budget_total(lg) == 75 and waivers.budget_left(db, A) == 75, "budget: a paid league's budget is its dues in dollars")
check(raises(waivers.place_claim, db, A, p[0], 80, now) is not None, "budget: bids can't exceed the dues")
db.commit()
resp = client.patch(f"/leagues/{lg.id}/settings", json={"acquisition_type": "full_waivers"}, headers=HDR)
check(resp.status_code == 200 and resp.json()["acquisition_type"] == "full_waivers", f"settings: type changes ({resp.status_code} {resp.text[:200]})")
resp = client.patch(f"/leagues/{lg.id}/settings", json={"acquisition_type": "bogus"}, headers=HDR)
check(resp.status_code == 422, "settings: unknown type refused")

print(f"OK - {checks} checks passed")
