"""
Pre-draft hub (platform/predraft_hub.py), through the real HTTP API (FastAPI TestClient) on a throwaway SQLite file
(predraft_hub_smoke_test.db):

  * GET /leagues/{id}/predraft-hub: draft time, size and rounds; a manual order gives exact pick numbers (snake), a random order
    only the rounds; a non-member gets no picks; recruiting leagues are refused; pick trading shown with the format's rule
  * player pool for ranking: filter, order, the caller's own rank
  * rankings: owner only, unknown / repeated players refused, replace and reorder
  * auto draft: the radio choice is the owner's alone; "My rankings" makes the system's pick for the team the first available
    ranked player, falls back to the FantasyMotion pick when the ranking has nobody left, "FantasyMotion" ignores the ranking,
    and AI-owned teams never use rankings
  * mock draft built to the league: size, rounds, the same draft position, rankings and choice carried over, the manager
    alone among AI teams, started, deletable

    .venv-api/Scripts/python.exe scripts/seed_predraft_hub_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

db_path = Path(os.environ.get("PREDRAFT_HUB_SMOKE_DB", "predraft_hub_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import predraft_hub
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, League, Player, RealTeam, Team, TeamDraftRanking, User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.platform.league_features import roster_spots
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()

nhl, _ = seed_hockey_reference_data(db)
real = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
db.add(real)
db.flush()
players = [Player(universe_id=nhl.id, real_team_id=real.id, full_name=f"Skater {i:03d}", position="C" if i % 2 else "W")
           for i in range(200)]
db.add_all(players)
db.flush()
assets = [DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id) for p in players]
db.add_all(assets)
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "other", "stranger")}
db.add_all(users.values())
db.commit()
asset_ids = [a.id for a in assets]
position_of = {a.id: a.player.position for a in assets}


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


HDR = {n: login(u) for n, u in users.items()}
checks = 0


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


SIZE = 4


def make_league(name, mode, positions=(3, 1, 4, 2)):
    body = {"name": name, "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"], "size": SIZE,
            "draft_order_mode": mode}
    resp = client.post("/leagues", json=body, headers=HDR["commish"])
    assert resp.status_code == 200, resp.text
    league = resp.json()
    items = [{"name": "Commish FC", "is_commissioner": True}] + [{"name": f"AI {i}", "is_ai": True} for i in range(SIZE - 1)]
    if mode == "manual":
        for item, pos in zip(items, positions):
            item["draft_position"] = pos
    resp = client.post(f"/leagues/{league['id']}/teams-setup", json={"teams": items}, headers=HDR["commish"])
    assert resp.status_code == 200, resp.text
    lid = league["id"]
    mine = db.query(Team).filter_by(league_id=lid, name="Commish FC").one()
    return lid, mine.id


def hub(lid, who="commish"):
    resp = client.get(f"/leagues/{lid}/predraft-hub", headers=HDR[who] if who else {})
    assert resp.status_code == 200, resp.text
    return resp.json()


lid, my_team = make_league("Manual league", "manual")
ROUNDS = roster_spots(db, lid)        # one round per roster spot
check(ROUNDS >= 12, f"the NHL roster has {ROUNDS} spots")
when = datetime.datetime.utcnow() + datetime.timedelta(days=3)
db.get(League, lid).draft_scheduled_at = when
db.commit()

# --- the hub: time, size, picks --------------------------------------------------------------------------------------
h = hub(lid)
check(h["draft_state"] == "not_started" and h["draft_id"] is None, "the draft hasn't started")
check(h["draft_scheduled_at"] is not None and h["draft_scheduled_at"].startswith(when.isoformat()[:16]),
      "the draft date and time are shown")
check(h["size"] == SIZE and h["rounds"] == ROUNDS and h["universe_codes"] == ["nhl"], f"size/rounds/sport: {h['size']} {h['rounds']}")
check(h["is_member"] and h["can_enter_room"], "a manager can enter the draft room")
check(h["room_url"] == f"/static/draft-board.html?league_id={lid}&team_id={my_team}", h["room_url"])
p = h["picks"]
check(p["order_known"] and len(p["picks"]) == ROUNDS and p["total_picks"] == ROUNDS * SIZE, "manual order: every pick is known")
check(p["picks"][0] == {"pick_number": 3, "round": 1, "made": False}, f"position 3 picks 3rd in round 1: {p['picks'][0]}")
check(p["picks"][1]["pick_number"] == 6, f"snake: position 3 of 4 is 6th overall in round 2: {p['picks'][1]}")
check(p["picks"][2]["pick_number"] == 11, "round 3 goes back to 3rd in the round")
check(h["teams"][0]["name"] == "AI 0" and h["teams"][0]["draft_position"] == 1, "teams listed in draft order")
check(h["trading"]["available"] is False and "random" in h["trading"]["reason"], "traditional redraft: no pick trading, with the reason")
check(h["rumors"]["available"] is False, "rumors are announced as coming later")
check(h["mock_draft"]["available"] and h["mock_draft"]["your_position"] == 3, "mock draft offered at the same position")

guest = hub(lid, None)
check(guest["is_member"] is False and guest["picks"]["picks"] == [] and guest["auto_draft"] is None, "a guest sees no picks")
check(guest["mock_draft"]["available"] is False, "a guest can't run the mock draft")
stranger = hub(lid, "stranger")
check(stranger["is_member"] is False and stranger["picks"]["picks"] == [], "a non-member sees no picks")

rid, rteam = make_league("Random league", "random")
r = hub(rid)["picks"]
check(r["order_known"] is False and len(r["picks"]) == ROUNDS and r["picks"][0]["pick_number"] is None, "random order: rounds only")
check("set when the draft starts" in r["order_note"], r["order_note"])
check(hub(rid)["mock_draft"]["your_position"] is None, "a random league's mock has a random position")
check(client.get("/leagues/99999/predraft-hub").status_code == 404, "unknown league")

# a format with pick trading shows the picks the team can trade
league_row = db.get(League, lid)
league_row.league_format = "dynasty"
db.commit()
t = hub(lid)["trading"]
check(t["available"] and len(t["picks"]) > 0 and t["trades_url"].endswith(f"&tab=trades&team_id={my_team}"), f"dynasty can trade picks: {t}")
league_row.pick_trading_enabled = False
db.commit()
check(hub(lid)["trading"]["available"] is False, "pick trading switched off by the commissioner")
league_row.pick_trading_enabled = True
league_row.league_format = "traditional_redraft"
db.commit()

# --- the pool for ranking -------------------------------------------------------------------------------------------
resp = client.get(f"/leagues/{lid}/predraft-hub/players?limit=50", headers=HDR["commish"])
pool = resp.json()
check(resp.status_code == 200 and pool["total"] == 200 and len(pool["players"]) == 50, "the pool pages")
resp = client.get(f"/leagues/{lid}/predraft-hub/players?position=C&q=skater 00", headers=HDR["commish"])
filt = resp.json()
check(filt["total"] == 5 and all(p["position"] == "C" for p in filt["players"]), f"position and name filter: {filt['total']}")

# --- rankings -------------------------------------------------------------------------------------------------------
best, worst, mid = asset_ids[0], asset_ids[-1], asset_ids[100]
check(client.put(f"/teams/{my_team}/draft-rankings", json={"draftable_asset_ids": [worst]}).status_code == 401, "sign-in needed")
check(client.put(f"/teams/{my_team}/draft-rankings", json={"draftable_asset_ids": [worst]}, headers=HDR["other"]).status_code == 403,
      "only the team's manager can rank")
check(client.get(f"/teams/{my_team}/draft-rankings", headers=HDR["other"]).status_code == 403, "nobody else reads the ranking")
check(client.put(f"/teams/{my_team}/draft-rankings", json={"draftable_asset_ids": [worst, worst]}, headers=HDR["commish"]).status_code == 400,
      "a repeat is refused")
check(client.put(f"/teams/{my_team}/draft-rankings", json={"draftable_asset_ids": [worst, 999999]}, headers=HDR["commish"]).status_code == 400,
      "a player outside the pool is refused")
resp = client.put(f"/teams/{my_team}/draft-rankings", json={"draftable_asset_ids": [worst, mid, best]}, headers=HDR["commish"])
check(resp.status_code == 200 and resp.json()["draftable_asset_ids"] == [worst, mid, best], resp.text)
resp = client.put(f"/teams/{my_team}/draft-rankings", json={"draftable_asset_ids": [mid, worst]}, headers=HDR["commish"])
check(resp.json()["draftable_asset_ids"] == [mid, worst], "a new ranking replaces the old one")
check(db.query(TeamDraftRanking).filter_by(team_id=my_team).count() == 2, "nothing of the old ranking is left")
resp = client.get(f"/leagues/{lid}/predraft-hub/players?limit=500", headers=HDR["commish"])
ranks = {p["draftable_asset_id"]: p["rank"] for p in resp.json()["players"] if p["rank"]}
check(ranks == {mid: 1, worst: 2}, f"the pool carries the caller's own rank: {ranks}")
resp = client.put(f"/teams/{my_team}/draft-rankings", json={"draftable_asset_ids": [worst, mid]}, headers=HDR["commish"])
check(resp.status_code == 200, resp.text)

# --- the auto-draft choice -------------------------------------------------------------------------------------------
check(hub(lid)["auto_draft"] == {"mode": "fantasymotion", "ranked_count": 2, "can_edit": True}, "default is FantasyMotion")
check(client.put(f"/teams/{my_team}/autodraft", json={"mode": "my_rankings"}, headers=HDR["other"]).status_code == 403, "owner only")
check(client.put(f"/teams/{my_team}/autodraft", json={"mode": "psychic"}, headers=HDR["commish"]).status_code == 422, "mode validated")
resp = client.put(f"/teams/{my_team}/autodraft", json={"mode": "my_rankings"}, headers=HDR["commish"])
check(resp.status_code == 200 and resp.json()["mode"] == "my_rankings", resp.text)
check(hub(lid)["auto_draft"]["mode"] == "my_rankings", "the choice is saved")

# --- the mock draft, built to the league (rankings and choice carried) ------------------------------------------------
check(client.post(f"/leagues/{lid}/predraft-hub/mock-draft", headers=HDR["other"]).status_code == 403, "only a manager can mock it")
check(client.post(f"/leagues/{lid}/predraft-hub/mock-draft", json={"pick_seconds": 5}, headers=HDR["commish"]).status_code == 422,
      "pick clock bounds")
resp = client.post(f"/leagues/{lid}/predraft-hub/mock-draft", headers=HDR["commish"])
check(resp.status_code == 200, resp.text)
mock = resp.json()
check(mock["your_position"] == 3, "the mock keeps the manager's draft position")
mock_row = db.get(League, mock["league_id"])
mteams = db.query(Team).filter_by(league_id=mock_row.id).all()
check(mock_row.is_mock_draft and len(mteams) == SIZE and mock_row.draft_rounds == ROUNDS, "same number of teams and rounds")
check(sum(1 for t in mteams if not t.is_ai_owned) == 1, "the manager is the only person")
mdraft = client.get(f"/drafts/league/{mock_row.id}").json()
check(mdraft["status"] == "in_progress", "the mock draft is running")
mpicks = client.get(f"/drafts/{mdraft['id']}/picks").json()
check(mpicks[2]["team_id"] == mock["team_id"] and mpicks[5]["team_id"] == mock["team_id"],
      "picks 3 and 6 are the manager's, as in the league")
check(predraft_hub.get_mode(db, mock["team_id"]) == "my_rankings" and predraft_hub.ranked_asset_ids(db, mock["team_id"]) == [worst, mid],
      "rankings and choice carried into the mock")
check(client.get(f"/leagues/{mock_row.id}/predraft-hub").status_code == 404, "a mock draft has no hub of its own")
mock_id, mock_team_id = mock_row.id, mock["team_id"]
resp = client.delete(f"/mock-drafts/{mock_id}", headers=HDR["commish"])
check(resp.status_code == 200, resp.text)
db.expire_all()
check(db.get(League, mock_id) is None and db.query(TeamDraftRanking).filter_by(team_id=mock_team_id).count() == 0,
      "deleting the mock removes its rankings")

# --- picks made for the team follow the choice -------------------------------------------------------------------------
resp = client.post("/drafts/start", json={"league_id": lid}, headers=HDR["commish"])
check(resp.status_code == 200, resp.text)
draft_id = resp.json()["id"]
live = hub(lid)
check(live["draft_state"] == "in_progress" and live["draft_id"] == draft_id, "the hub follows the draft once it starts")
check(live["picks"]["picks"][0]["pick_number"] == 3, "pick numbers come from the real draft")


def autopick():
    resp = client.post(f"/drafts/{draft_id}/autopick", headers=HDR["commish"])
    assert resp.status_code == 200, resp.text
    return resp.json()


autopick()
autopick()        # AI teams on picks 1 and 2
got = autopick()
check(got["team_id"] == my_team and got["draftable_asset_id"] == worst and got["is_autopick"],
      f"My rankings: pick 3 is the top-ranked player (the last on the board by points): {got}")
check(hub(lid)["picks"]["picks"][0]["made"] is True, "the made pick is marked")

autopick()
autopick()        # picks 4 and 5
got = autopick()
check(got["team_id"] == my_team and got["draftable_asset_id"] == mid, "pick 6: the next player in the ranking")
for _ in range(4):
    autopick()    # picks 7 to 10
got = autopick()
check(got["team_id"] == my_team, "pick 11 is mine")
check(got["draftable_asset_id"] not in {worst, mid}, "ranking used up: FantasyMotion's pick fills in, the draft doesn't stall")

client.put(f"/teams/{my_team}/draft-rankings", json={"draftable_asset_ids": [asset_ids[150]]}, headers=HDR["commish"])
client.put(f"/teams/{my_team}/autodraft", json={"mode": "fantasymotion"}, headers=HDR["commish"])
for _ in range(2):
    autopick()    # picks 12 and 13
got = autopick()
check(got["team_id"] == my_team and got["draftable_asset_id"] != asset_ids[150], "the FantasyMotion setting ignores the ranking")

ai_team = db.query(Team).filter_by(league_id=lid, is_ai_owned=True).first()
db.add(TeamDraftRanking(team_id=ai_team.id, draftable_asset_id=asset_ids[5], rank=1))
predraft_hub.set_mode(db, ai_team, "my_rankings")
db.commit()
check(predraft_hub.ranked_choice(db, ai_team, set(asset_ids)) is None, "an AI-owned team ignores rankings")
check(predraft_hub.ranked_choice(db, db.get(Team, my_team), set(asset_ids)) is None, "FantasyMotion setting gives no ranked choice")

# --- recruiting leagues have no hub ----------------------------------------------------------------------------------
rec_lid, _ = make_league("Recruit", "random")
db.get(League, rec_lid).draft_type = "recruit"
db.commit()
check(client.get(f"/leagues/{rec_lid}/predraft-hub").status_code == 400, "recruiting leagues use the recruiting board")

print(f"pre-draft hub smoke test: {checks} checks passed")
db.close()
engine.dispose()
if db_path.exists():
    db_path.unlink()
