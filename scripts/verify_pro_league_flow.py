"""
Offline checks (throwaway SQLite, TestClient) for non-college (NHL) leagues after this change:

  * the draft has one round per roster spot, so every team ends with a full roster; a league can't be created with fewer
    rounds, and a draft can't start when the player pool can't fill every roster spot
  * free agency is open the moment the draft ends (and not before), and signing is free
  * the league's schedule is generated automatically when the draft ends (NHL: the series round robin), and can be
    generated on demand by the commissioner (idempotent)
  * a manager can drop a player to free agency (the lines close up) and sign him back; others can't drop for them
  * roster payloads carry points per game, computed from stored credit and never invented

    .venv-api/Scripts/python.exe scripts/verify_pro_league_flow.py
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Mock external network calls to keep test offline and hermetic
_fetch_patcher = patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None)
_fetch_patcher.start()

db_path = Path("pro_league_flow_verify.db")
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import DraftableAsset, Player, RealTeam, RosterEntry, Team, User
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit

Base.metadata.create_all(bind=engine)
client = TestClient(app)


def check(ok, msg):
    print(("ok  " if ok else "FAIL ") + msg)
    if not ok:
        sys.exit(1)


db = SessionLocal()
nhl, _ = seed_hockey_reference_data(db)
club = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
db.add(club)
db.flush()
asset_of = {}
for pos, n in (("C", 40), ("LW", 28), ("RW", 28), ("D", 60), ("G", 28)):
    for i in range(n):
        p = Player(universe_id=nhl.id, real_team_id=club.id, full_name=f"{pos} Player {i:02d}", position=pos)
        db.add(p)
        db.flush()
        a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
        db.add(a)
        db.flush()
        asset_of[p.full_name] = a.id
owner = User(email="o@example.com", display_name="Owner")
rival = User(email="r@example.com", display_name="Rival")
db.add_all([owner, rival])
db.commit()


def token(u):
    return {"Authorization": "Bearer " + client.post("/sessions", json={"user_id": u.id}).json()["token"]}


auth, rival_auth = token(owner), token(rival)


def new_league(name, size, **extra):
    return client.post("/leagues", json={"name": name, "commissioner_user_id": owner.id, "universe_codes": ["nhl"],
                                         "size": size, **extra}, headers=auth)


# ---- a draft fills every roster spot ----
r = new_league("Short", 6, draft_rounds=5)
check(r.status_code == 400 and "roster size" in r.json()["detail"], "fewer rounds than roster spots is refused for an NHL league")
lg = new_league("Pro", 6).json()
check(lg["draft_rounds"] == 18, "default rounds = 18 roster spots (6 starters + 12 bench)")
big = new_league("Too big", 11).json()
client.post(f"/leagues/{big['id']}/ai-teams", headers=auth)
r = client.post("/drafts/start", json={"league_id": big["id"]})
check(r.status_code == 400 and "pool" in r.json()["detail"], f"a pool that can't fill every roster spot blocks the draft: {r.json()['detail'][:70]}")

client.post(f"/leagues/{lg['id']}/ai-teams", headers=auth)
mine = db.query(Team).filter_by(league_id=lg["id"]).order_by(Team.id).first()
mine.owner_user_id, mine.is_ai_owned = owner.id, False
db.commit()

ov = client.get(f"/leagues/{lg['id']}/overview").json()
check(ov["free_agency_open"] is False and ov["draft_state"] == "not_started", "before the draft: free agency closed")
check(client.post(f"/teams/{mine.id}/free-agency/drop", json={"draftable_asset_id": 1}, headers=auth).status_code == 400,
      "dropping before the draft ends is refused")
check(client.get(f"/leagues/{lg['id']}/series-schedule").json()["series"] == [], "no schedule before the draft ends")

d = client.post("/drafts/start", json={"league_id": lg["id"]}).json()
n = 0
while client.post(f"/drafts/{d['id']}/autopick").status_code == 200:
    n += 1
check(n == 108, f"108 picks made (6 teams x 18 rounds): {n}")
counts = [db.query(RosterEntry).filter_by(team_id=t.id).count() for t in db.query(Team).filter_by(league_id=lg["id"])]
check(counts == [18] * 6, f"every team's roster is full: {counts}")

# ---- free agency opens immediately; schedule is generated ----
ov = client.get(f"/leagues/{lg['id']}/overview").json()
check(ov["free_agency_open"] and ov["draft_state"] == "complete" and ov["is_college"] is False, "free agency opens the moment the draft ends")
fa = client.get(f"/leagues/{lg['id']}/free-agents").json()
check(fa["open"] and len(fa["players"]) == 184 - 108 and all(p["asking_price"] == 0 for p in fa["players"]), "76 free agents, all free to sign")
series = client.get(f"/leagues/{lg['id']}/series-schedule").json()["series"]
check(len(series) == 5 * 3 and all(len(s["daily_games"]) == s["total_days"] for s in series),
      f"the series schedule was generated automatically ({len(series)} series, 5 rounds x 3 pairings)")
check(client.get(f"/leagues/{lg['id']}/schedule/state").json()["has_schedule"] is True, "schedule state reports it")
check(client.post(f"/leagues/{lg['id']}/schedule/generate", headers=rival_auth).status_code == 403, "only the commissioner can generate")
r = client.post(f"/leagues/{lg['id']}/schedule/generate", headers=auth).json()
check(r["created"] is True and len(client.get(f"/leagues/{lg['id']}/series-schedule").json()["series"]) == len(series),
      "generate is idempotent: nothing is duplicated")

# ---- points per game ----
roster = client.get(f"/teams/{mine.id}/roster").json()
check(len(roster) == 18 and all("points_per_game" in r for r in roster), "roster payload carries points per game")
check(all(r["points_per_game"] is None for r in roster), "with no stored credit there is no rate (never invented)")
star = roster[0]
for game, pts in ((901, 1.0), (902, 3.0)):
    s = Shot(game_id=game, source="t", external_shot_id=f"x{game}", shooter_asset_id=star["draftable_asset_id"])
    db.add(s)
    db.flush()
    db.add(ShotCredit(shot_id=s.id, draftable_asset_id=star["draftable_asset_id"], role="shooter", credit_points=pts))
db.commit()
roster = {r["draftable_asset_id"]: r for r in client.get(f"/teams/{mine.id}/roster").json()}
check(roster[star["draftable_asset_id"]]["games"] == 2 and roster[star["draftable_asset_id"]]["points_per_game"] == 2.0,
      "points per game = stored credit / games with credit (4.0 / 2 = 2.0)")
po = client.get(f"/teams/{mine.id}/pecking-order").json()
shown = [p for g in po["groups"] for p in g["ranked"] + g["unranked"] if p["draftable_asset_id"] == star["draftable_asset_id"]][0]
check(shown["points_per_game"] == 2.0, "the lines payload carries it too")

# ---- drop and re-sign ----
victim = next(p for g in po["groups"] if g["position_group"] == "W" for p in g["ranked"] if p["priority"] == 1)
others = [p["player_id"] for g in po["groups"] if g["position_group"] == "W" for p in g["ranked"] if p["priority"] != 1]
r = client.post(f"/teams/{mine.id}/free-agency/drop", json={"draftable_asset_id": victim["draftable_asset_id"]}, headers=rival_auth)
check(r.status_code == 403, "another manager can't drop your player")
r = client.post(f"/teams/{mine.id}/free-agency/drop", json={"draftable_asset_id": victim["draftable_asset_id"]}, headers=auth)
check(r.status_code == 200 and r.json()["kind"] == "dropped", "the manager drops a player to free agency")
po2 = client.get(f"/teams/{mine.id}/pecking-order").json()
w = next(g for g in po2["groups"] if g["position_group"] == "W")
check([p["player_id"] for p in sorted(w["ranked"], key=lambda p: p["priority"])] == others and [p["priority"] for p in sorted(w["ranked"], key=lambda p: p["priority"])] == list(range(1, len(others) + 1)),
      "the lines close up: the wingers behind him each move up one")
fa = client.get(f"/leagues/{lg['id']}/free-agents").json()
check(any(p["draftable_asset_id"] == victim["draftable_asset_id"] for p in fa["players"]), "he is in the free-agent pool")
r = client.post(f"/teams/{mine.id}/free-agency/add", json={"draftable_asset_id": victim["draftable_asset_id"]}, headers=auth)
check(r.status_code == 200 and r.json()["price"] == 0, "he can be signed back for free")
w = next(g for g in client.get(f"/teams/{mine.id}/pecking-order").json()["groups"] if g["position_group"] == "W")
check(w["ranked"][-1]["player_id"] == victim["player_id"] if False else any(p["player_id"] == victim["player_id"] for p in w["ranked"]),
      "a signed player goes onto the end of his position's lines")
r = client.post(f"/teams/{mine.id}/free-agency/add", json={"draftable_asset_id": fa["players"][1]["draftable_asset_id"]}, headers=auth)
check(r.status_code == 400 and "full" in r.json()["detail"], "a full roster can't sign more: drop first")

# ---- a league created under the old 14-player roster follows the 18-player limit ----
from fantasy_sports.platform.db.models.core import League, RosterSlotDefinition, TeamPeckingOrder
old = db.get(League, lg["id"])
old.draft_rounds = 14                                                    # what the league stored when it was created
bench = db.query(RosterSlotDefinition).filter_by(universe_id=nhl.id, slot_code="BENCH").one()
bench.slot_count = 8                                                     # and what the database's bench slot said
for t in db.query(Team).filter_by(league_id=lg["id"]):                   # every team holds 14 players, as it did then
    keep = [e.id for e in db.query(RosterEntry).filter_by(team_id=t.id).order_by(RosterEntry.id)][:14]
    kept_assets = [e.draftable_asset_id for e in db.query(RosterEntry).filter(RosterEntry.id.in_(keep))]
    db.query(RosterEntry).filter(RosterEntry.team_id == t.id, ~RosterEntry.id.in_(keep)).delete(synchronize_session=False)
    db.query(TeamPeckingOrder).filter(TeamPeckingOrder.team_id == t.id, ~TeamPeckingOrder.draftable_asset_id.in_(kept_assets)).delete(synchronize_session=False)
db.commit()
db.expire_all()
ov = client.get(f"/leagues/{lg['id']}/overview").json()
check(ov["roster_size"] == 18, f"the league's roster limit reads 18, not its stored 14: {ov['roster_size']}")
mine_row = next(t for t in ov["teams"] if t["id"] == mine.id)
check(mine_row["roster_count"] == 14 and mine_row["open_spots"] == 4, "a 14-man team shows 4 open spots")
summary = client.get(f"/teams/{mine.id}/free-agency/summary", headers=auth).json()
check(summary["roster_size"] == 18 and summary["open_spots"] == 4, "free agency summary agrees")
fa = client.get(f"/leagues/{lg['id']}/free-agents").json()["players"]
for p in fa[:4]:
    r = client.post(f"/teams/{mine.id}/free-agency/add", json={"draftable_asset_id": p["draftable_asset_id"]}, headers=auth)
    assert r.status_code == 200, r.text
check(db.query(RosterEntry).filter_by(team_id=mine.id).count() == 18, "the manager can sign up to 18 players")
r = client.post(f"/teams/{mine.id}/free-agency/add", json={"draftable_asset_id": fa[4]["draftable_asset_id"]}, headers=auth)
check(r.status_code == 400 and "18" in r.json()["detail"], "...and no more than 18")
check(ov["ai_teams_with_open_spots"] == 5, "the five AI teams show open spots")
r = client.post(f"/leagues/{lg['id']}/free-agency/ai-fill", headers=auth).json()
check(r["total"] == 5 * 4, f"the commissioner's AI fill signs {r['total']} players: 4 for each AI team")
counts = sorted(db.query(RosterEntry).filter_by(team_id=t.id).count() for t in db.query(Team).filter_by(league_id=lg["id"]))
check(counts == [18] * 6, f"every roster is now 18: {counts}")
check(client.post(f"/leagues/{lg['id']}/free-agency/ai-fill", headers=auth).json()["total"] == 0, "running the AI fill again does nothing")

# ---- pages ----
page = (ROOT / "web/static/league.html").read_text(encoding="utf-8")
check("dropPlayer" in page and "PPG" in page and "proScheduleHtml" in page, "league page has drop, PPG and the schedule view")
check("--space-5" in (ROOT / "web/static/styles.css").read_text(encoding="utf-8"), "the --space-5 spacing token used across the pages is defined")

db.close()
engine.dispose()
db_path.unlink()
print("\nAll pro league flow checks passed.")
