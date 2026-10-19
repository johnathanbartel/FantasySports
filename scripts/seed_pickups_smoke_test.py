"""
Trending and recommended pickups (NHL), on a throwaway SQLite file (pickups_smoke_test.db):
the pure maths (trend percentage, starter baselines, ranking by points above the baseline), then
GET /leagues/{id}/trending-pickups and GET /teams/{id}/recommended-pickups through the real HTTP API (FastAPI TestClient).

    .venv-api/Scripts/python.exe scripts/seed_pickups_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("PICKUPS_SMOKE_DB", "pickups_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import free_agency, hockey_projection as hp, pickups, waivers
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    Draft, DraftableAsset, FreeAgencyTransaction, Game, League, Player, RealTeam, RosterEntry, Team, User, Week,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.draft_stats import nhl_season_year
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit

checks = 0


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


# ---- pure ------------------------------------------------------------------------------------------------------------
check(pickups.trend_percent(2, 3) == 66.7 and pickups.trend_percent(0, 5) == 0.0 and pickups.trend_percent(1, 0) == 0.0, "trend percentage")
mk = lambda aid, grp, pts: hp.Projection(aid, grp, 25, 82, pts / 82, pts, "history", 1)   # noqa: E731
base = pickups.starter_baselines([mk(1, "W", 50), mk(2, "W", 30), mk(3, "W", 10), mk(4, "G", 40)])
check(base == {"C": 0.0, "W": 30.0, "D": 0.0, "G": 40.0}, "baseline = weakest starter; an unfilled slot is 0")
check(pickups.starter_baselines([mk(1, "W", 50)])["W"] == 0.0, "one winger leaves the second wing slot open")
cands = [(10, mk(10, "W", 45)), (11, mk(11, "W", 20)), (12, mk(12, "G", 70)), (13, mk(13, "C", 5))]
rec = pickups.recommend(cands, base, {"W": 3, "G": 1})
check([r["asset_id"] for r in rec] == [12, 10, 13] and [r["value"] for r in rec] == [30.0, 15.0, 5.0],
      "best value first; the 20-point winger is below the 30-point starter and is left out")
check({r["asset_id"]: r["fills_open_slot"] for r in rec} == {10: False, 12: False, 13: True}, "an unfilled starting slot is flagged")

# ---- database + API --------------------------------------------------------------------------------------------------
Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()
nhl, _ = seed_hockey_reference_data(db)
rt = [RealTeam(universe_id=nhl.id, name=f"Team {i}", external_ref=f"T{i}") for i in range(2)]
db.add_all(rt)
db.flush()
target = nhl_season_year()
seasons = [target - 1, target - 2, target - 3]
games_by_season = {}
for s in seasons:
    w = Week(universe_id=nhl.id, season_year=s, week_number=1)
    db.add(w)
    db.flush()
    gl = [Game(universe_id=nhl.id, week_id=w.id, home_team_id=rt[0].id, away_team_id=rt[1].id, status="final") for _ in range(20)]
    db.add_all(gl)
    db.flush()
    games_by_season[s] = [g.id for g in gl]

POS = ["C", "W", "W", "D", "D", "G"]
assets = []
for i in range(24):
    p = Player(universe_id=nhl.id, real_team_id=rt[i % 2].id, full_name=f"Player {i:02d}", position=POS[i % 6],
               birth_date=datetime.date(target - 26, 3, 1), external_ref=f"P{i}")
    db.add(p)
    db.flush()
    a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
    db.add(a)
    db.flush()
    assets.append(a)
    for s in seasons:
        for n, gid in enumerate(games_by_season[s]):
            shot = Shot(game_id=gid, source="test", external_shot_id=f"{a.id}-{s}-{n}", shooter_asset_id=a.id)
            db.add(shot)
            db.flush()
            db.add(ShotCredit(shot_id=shot.id, draftable_asset_id=a.id, role="shooter", credit_points=0.60 - 0.02 * i))
user = User(email="commish@example.com", display_name="commish")
db.add(user)
db.commit()
proj = hp.project_pool(db)
check(len(proj) == 24, "every player is projected")

token = client.post("/sessions", json={"user_id": user.id}).json()["token"]
hdr = {"Authorization": f"Bearer {token}"}


def new_league(name, draft_status="complete"):
    resp = client.post("/leagues", json={"name": name, "commissioner_user_id": user.id, "universe_codes": ["nhl"], "size": 4}, headers=hdr)
    assert resp.status_code == 200, resp.text
    lid = resp.json()["id"]
    ts = [Team(league_id=lid, name=f"{name}{n}", owner_user_id=None, is_ai_owned=True) for n in range(2)]
    db.add_all(ts)
    if draft_status:
        db.add(Draft(league_id=lid, status=draft_status, is_snake=True))
    db.commit()
    return lid, [t.id for t in ts]


home, (t1, t2) = new_league("Home")
# t1 has only the best center; t2 has only the worst goalie
db.add_all([RosterEntry(team_id=t1, draftable_asset_id=assets[0].id, acquired_via="draft"),
            RosterEntry(team_id=t2, draftable_asset_id=assets[23].id, acquired_via="draft")])
db.commit()

# ---- trending -------------------------------------------------------------------------------------------------------
r = client.get(f"/leagues/{home}/trending-pickups").json()
check(r["available"] is False and r["players"] == [] and "other leagues" in r["reason"].lower(), "no other leagues: nothing to compare, says why")
check(client.get("/leagues/99999/trending-pickups").status_code == 404, "unknown league: 404")

lb, (b1, b2) = new_league("B")
lc, (c1, c2) = new_league("C")
ld, (d1, _) = new_league("D")
le, (e1, _) = new_league("Mock")
db.get(League, le).is_mock_draft = True
db.commit()
now = datetime.datetime.utcnow()


def add(league, team, asset, reason="user", days_ago=1.0, kind="add"):
    db.add(FreeAgencyTransaction(league_id=league, team_id=team, draftable_asset_id=asset.id, kind=kind, price=0, reason=reason,
                                 created_at=now - datetime.timedelta(days=days_ago)))


X, Y, Z, W, OLD, OWN, RO, DROP = (assets[i] for i in (6, 7, 8, 9, 10, 11, 12, 13))
add(lb, b1, X); add(lc, c1, X); add(lc, c2, X)                  # X: two leagues (one of them twice)
add(lb, b2, Y, reason="waiver")                                 # Y: a waiver win in one league
add(ld, d1, Z, reason="ai_fill")                                # not a decision
add(le, e1, W)                                                  # mock draft
add(lb, b1, OLD, days_ago=10)                                   # too long ago
add(home, t1, OWN)                                              # this league's own add is not "other leagues"
add(lc, c1, RO)                                                 # trending elsewhere, but rostered here
add(ld, d1, DROP, kind="drop")                                  # a drop is not an add
db.add(RosterEntry(team_id=t2, draftable_asset_id=RO.id, acquired_via="draft"))
db.commit()

r = client.get(f"/leagues/{home}/trending-pickups").json()
ids = [p["asset_id"] for p in r["players"]]
check(r["available"] and r["other_leagues"] == 3 and r["days"] == 7, "three real other leagues are compared (the mock draft is not)")
check(ids == [X.id, Y.id], f"trending: X then Y only, got {ids}")
check(r["players"][0]["leagues_adding"] == 2 and r["players"][0]["leagues_total"] == 3 and r["players"][0]["percent"] == 66.7,
      "a league counts once however often it added him: 2 of 3 leagues")
check(r["players"][1]["percent"] == 33.3 and r["players"][0]["name"] == "Player 06", "Y is 1 of 3; rows carry the player")
check(not ({Z.id, W.id, OLD.id, OWN.id, RO.id, DROP.id} & set(ids)), "AI fill, mock drafts, old adds, this league, rostered players and drops are left out")
# a player is no longer trending once someone here signs him
db.add(RosterEntry(team_id=t2, draftable_asset_id=Y.id, acquired_via="free_agency"))
db.commit()
check([p["asset_id"] for p in client.get(f"/leagues/{home}/trending-pickups").json()["players"]] == [X.id], "signed here: off the list")

# ---- recommended ----------------------------------------------------------------------------------------------------
r = client.get(f"/teams/{t1}/recommended-pickups").json()
rows = r["recommendations"]
check(r["available"] and 0 < len(rows) <= pickups.RECOMMENDED_LIMIT, "recommendations for a team with open starting spots")
check([x["value"] for x in rows] == sorted((x["value"] for x in rows), reverse=True) and all(x["value"] > 0 for x in rows), "best value first, upgrades only")
check(not ({a.id for a in (assets[0], assets[23], RO)} & {x["asset_id"] for x in rows}), "rostered players are never recommended")
check(all(x["group"] != "C" for x in rows), "no free center beats the team's best center")
check(all(x["fills_open_slot"] and x["baseline_points"] == 0.0 and abs(x["value"] - x["projected_points"]) < 0.11 for x in rows),
      "an open slot has baseline 0, so the value is the player's projected points")
best_w = max((a for a in assets if POS[assets.index(a) % 6] == "W" and a.id not in {RO.id, Y.id}), key=lambda a: proj[a.id].points)
check(any(x["asset_id"] == best_w.id for x in rows), "the best free winger is recommended")
check(all("name" in x and "waiver_until" in x for x in rows), "rows carry the player and waiver state")

r = pickups.recommended(db, db.get(Team, t2), limit=40)      # the page shows the top few overall; ask for more to see every group
goalies = [x for x in r["recommendations"] if x["group"] == "G"]
check(goalies and goalies[0]["asset_id"] == assets[5].id, "the best free goalie leads the goalie upgrades over the worst one rostered")
check(not goalies[0]["fills_open_slot"] and abs(goalies[0]["baseline_points"] - proj[assets[23].id].points) < 0.11, "baseline is the weakest starter")
check(abs(goalies[0]["value"] - (proj[assets[5].id].points - proj[assets[23].id].points)) < 0.11, "value = points above the player he replaces")

check(client.get("/teams/99999/recommended-pickups").status_code == 404, "unknown team: 404")
lf, (f1, _) = new_league("NoDraft", draft_status=None)
r = client.get(f"/teams/{f1}/recommended-pickups").json()
check(r["available"] is False and "draft" in r["reason"].lower(), "free agency closed: unavailable, with a reason")

# the engine shared with pick suggestions still reads the same counts
from fantasy_sports.platform import draft_grades
check(draft_grades.roster_group_counts(db, {assets[0].id, assets[1].id, assets[5].id}, proj) == {"C": 1, "W": 1, "G": 1}, "shared roster group counts")

print(f"pickups smoke test OK ({checks} checks)")
