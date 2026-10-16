"""
Trade Hub and Trade Block, through the real HTTP API (FastAPI TestClient) on a throwaway SQLite file
(trade_hub_smoke_test.db): the deadline, manager plans, AI teams turning Rebuilding only once mathematically eliminated,
and the trade block. The elimination rule itself is also checked on its own (pure function).

    .venv-api/Scripts/python.exe scripts/seed_trade_hub_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("TRADE_HUB_SMOKE_DB", "trade_hub_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import trade_hub
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, League, LeagueSeries, Player, RealTeam, RosterEntry, SeriesDailyGame, Team, User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

checks = 0


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


# --- the elimination rule on its own -------------------------------------------------------------------------------------
# 4 teams, 2 playoff spots. best / worst = the most / least a team can finish with.
best = {1: 30, 2: 24, 3: 12, 4: 9}
worst = {1: 21, 2: 15, 3: 6, 4: 3}
check(trade_hub.eliminated_from(best, worst, {}, 2) == {3, 4}, "teams with two others guaranteed above even their best finish are out")
check(trade_hub.eliminated_from({1: 30, 2: 24, 3: 15, 4: 9}, worst, {}, 2) == {4},
      "a tie with a guaranteed finisher is not elimination (tiebreakers could still favour it)")
check(trade_hub.eliminated_from(best, worst, {1: 7, 2: 7, 3: 8, 4: 8}, 2) == set(),
      "teams 3 and 4 are out of the rank order but each can still win the weaker division, and a division winner is seeded in")
check(trade_hub.eliminated_from(best, worst, {1: 7, 3: 7, 2: 8, 4: 8}, 2) == {3, 4},
      "a team whose division already has a team guaranteed above its best finish is out")
check(trade_hub.eliminated_from({1: 9, 2: 9}, {1: 0, 2: 0}, {}, 2) == set(), "nobody is out while there are spots for everyone")

# --- the API ---------------------------------------------------------------------------------------------------------------
Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()

nhl, _ = seed_hockey_reference_data(db)
real = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
db.add(real)
db.flush()
players = [Player(universe_id=nhl.id, real_team_id=real.id, full_name=f"Skater {i:03d}", position="C") for i in range(6)]
db.add_all(players)
db.flush()
assets = [DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id) for p in players]
db.add_all(assets)
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "a", "b", "stranger")}
db.add_all(users.values())
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


HDR = {n: login(u) for n, u in users.items()}

resp = client.post("/leagues", json={"name": "Hub", "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"], "size": 4},
                   headers=HDR["commish"])
assert resp.status_code == 200, resp.text
lid = resp.json()["id"]
team_a = Team(league_id=lid, name="Alpha", owner_user_id=users["a"].id)
team_b = Team(league_id=lid, name="Bravo", owner_user_id=users["b"].id)
team_c = Team(league_id=lid, name="Charlie AI", owner_user_id=None, is_ai_owned=True)
team_d = Team(league_id=lid, name="Delta AI", owner_user_id=None, is_ai_owned=True)
db.add_all([team_a, team_b, team_c, team_d])
db.commit()
A, B, C, D = team_a.id, team_b.id, team_c.id, team_d.id


def hub():
    resp = client.get(f"/leagues/{lid}/trade-hub")
    assert resp.status_code == 200, resp.text
    return resp.json(), {t["team_id"]: t for t in resp.json()["teams"]}


# Everyone starts Win Now; no deadline yet.
body, by = hub()
check(body["trade_deadline"] is None and body["deadline_passed"] is False, "no deadline set: shown as none, not passed")
check(all(t["plan"] == "win_now" for t in by.values()), "everyone defaults to Win Now")

# Schedule: one single-game series per game. A beat D three times; B beat C once; A vs B is still to play (so are Charlie's and
# Delta's games with nobody - they have no games left).
day = [datetime.date(2026, 10, 1)]


def game(home, away, home_score=None, away_score=None):
    day[0] += datetime.timedelta(days=1)
    s = LeagueSeries(league_id=lid, team_a_id=home, team_b_id=away, start_date=day[0], end_date=day[0], total_days=5)
    db.add(s)
    db.flush()
    final = home_score is not None
    db.add(SeriesDailyGame(series_id=s.id, calendar_date=day[0], home_team_id=home, away_team_id=away,
                           home_score=home_score or 0, away_score=away_score or 0, status="FINAL" if final else "SCHEDULED"))


# Early season: nothing played, three games each still to come -> nobody can be out yet.
for _ in range(3):
    game(A, B)
    game(C, D)
    game(A, C)
    game(B, D)
db.commit()
body, by = hub()
check(all(t["plan"] == "win_now" and not t["eliminated"] for t in by.values()), "early season: AI teams stay Win Now")

# Late season: replace the schedule with a nearly finished one.
db.query(SeriesDailyGame).delete()
db.query(LeagueSeries).delete()
db.commit()
day[0] = datetime.date(2026, 10, 1)
for _ in range(3):
    game(A, D, 3, 1)      # A beats D three times: 9 points
game(B, C, 2, 1)          # B beats C: 3 points
game(A, B)                # the only game left: A v B
db.commit()
# Default playoff field = 33% of 4 teams = 1 spot. A has 9 and B can reach at most 6; C and D have nothing left to play for.
body, by = hub()
check(by[C]["eliminated"] and by[C]["plan"] == "rebuilding", "an AI team that can no longer make the playoffs is Rebuilding")
check(by[D]["eliminated"] and by[D]["plan"] == "rebuilding", "the other AI team too")
check(by[B]["eliminated"] and by[B]["plan"] == "win_now", "a manager's team that is eliminated keeps the plan the manager chose")
check(not by[A]["eliminated"] and by[A]["plan"] == "win_now", "the leader is not eliminated")

# Offseason: nobody is eliminated.
league = db.get(League, lid)
league.offseason = True
db.commit()
body, by = hub()
check(all(not t["eliminated"] and t["plan"] == "win_now" for t in by.values() if t["is_ai_owned"]), "offseason: AI teams are Win Now again")
league.offseason = False
db.commit()

# A manager picks a plan.
resp = client.put(f"/teams/{A}/trade-plan", json={"plan": "rebuilding"}, headers=HDR["a"])
check(resp.status_code == 200 and resp.json()["plan"] == "rebuilding", "the manager can pick Rebuilding")
check(hub()[1][A]["plan"] == "rebuilding", "the hub shows the manager's pick")
check(client.put(f"/teams/{A}/trade-plan", json={"plan": "win_now"}, headers=HDR["stranger"]).status_code == 403, "only the owner can set the plan")
check(client.put(f"/teams/{A}/trade-plan", json={"plan": "win_now"}).status_code in (401, 403), "signing in is required")
check(client.put(f"/teams/{A}/trade-plan", json={"plan": "tanking"}, headers=HDR["a"]).status_code == 400, "only the two plans exist")
check(client.put(f"/teams/{C}/trade-plan", json={"plan": "win_now"}, headers=HDR["commish"]).status_code in (400, 403),
      "an AI team's plan can't be set by anyone")
check(hub()[1][C]["plan"] == "rebuilding", "the AI team's plan still follows its elimination")

# Trade block.
db.add_all([RosterEntry(team_id=A, draftable_asset_id=assets[0].id), RosterEntry(team_id=A, draftable_asset_id=assets[1].id),
            RosterEntry(team_id=B, draftable_asset_id=assets[2].id)])
db.commit()
check(client.get(f"/leagues/{lid}/trade-block").json() == [], "the block starts empty")
resp = client.post(f"/teams/{A}/trade-block", json={"draftable_asset_id": assets[0].id}, headers=HDR["a"])
check(resp.status_code == 200 and resp.json()["display_name"] == "Skater 000", "a manager can put a player on the block")
client.post(f"/teams/{A}/trade-block", json={"draftable_asset_id": assets[0].id}, headers=HDR["a"])
check(client.post(f"/teams/{A}/trade-block", json={"draftable_asset_id": assets[2].id}, headers=HDR["a"]).status_code == 400,
      "a player on someone else's roster can't be put on the block")
check(client.post(f"/teams/{A}/trade-block", json={"draftable_asset_id": assets[1].id}, headers=HDR["b"]).status_code == 403,
      "only the owner can change the block")
block = client.get(f"/leagues/{lid}/trade-block").json()
check(len(block) == 1 and block[0]["team_id"] == A and [p["draftable_asset_id"] for p in block[0]["players"]] == [assets[0].id],
      "the block lists the player once, under his team")
check(block[0]["plan"] == "rebuilding" and hub()[1][A]["on_block"] == 1, "the block shows the team's plan and the hub counts it")
check(client.delete(f"/teams/{A}/trade-block/{assets[0].id}", headers=HDR["b"]).status_code == 403, "another manager can't remove him")

client.post(f"/teams/{A}/trade-block", json={"draftable_asset_id": assets[1].id}, headers=HDR["a"])
db.query(RosterEntry).filter_by(team_id=A, draftable_asset_id=assets[1].id).delete()
db.commit()
block = client.get(f"/leagues/{lid}/trade-block").json()
check([p["draftable_asset_id"] for p in block[0]["players"]] == [assets[0].id], "a player who has left the roster drops off the block")
check(client.delete(f"/teams/{A}/trade-block/{assets[0].id}", headers=HDR["a"]).status_code == 200, "the owner can take him off")
check(client.get(f"/leagues/{lid}/trade-block").json() == [], "the block is empty again")

# The deadline shows, and shows as passed once it has.
league.trade_deadline = datetime.datetime.utcnow() + datetime.timedelta(days=3)
db.commit()
body, _ = hub()
check(body["trade_deadline"] is not None and body["deadline_passed"] is False, "a future deadline is shown, not passed")
league.trade_deadline = datetime.datetime.utcnow() - datetime.timedelta(days=1)
db.commit()
check(hub()[0]["deadline_passed"] is True, "a past deadline shows as passed")
check(client.get("/leagues/99999/trade-hub").status_code == 404, "unknown league is a 404")

print(f"trade hub smoke test: {checks} checks passed")
db.close()
engine.dispose()
db_path.unlink(missing_ok=True)
