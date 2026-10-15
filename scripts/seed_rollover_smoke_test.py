"""
Round-trips season rollover through the real HTTP API (FastAPI TestClient) on a throwaway SQLite file
(rollover_smoke_test.db): for each League Format, finish a snake draft, roll the season over, and check what carries
over, the roster freeze, the next draft's order (random / reverse standings, snake / not), and the archive.

    .venv-api/Scripts/python.exe scripts/seed_rollover_smoke_test.py
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("ROLLOVER_SMOKE_DB", "rollover_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import DraftableAsset, Player, RealTeam, RosterEntry, Team, User
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
commissioner = User(email="commish@example.com", display_name="Commish")
other = User(email="other@example.com", display_name="Other")
db.add_all([commissioner, other])
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


H, H_OTHER = login(commissioner), login(other)


def make_league(name, fmt):
    resp = client.post("/leagues", json={"name": name, "commissioner_user_id": commissioner.id, "universe_codes": ["nhl"],
                                          "size": 4}, headers=H)
    assert resp.status_code == 200, resp.text
    league = resp.json()
    assert client.patch(f"/leagues/{league['id']}/settings", json={"league_format": fmt}, headers=H).status_code == 200
    teams = [Team(league_id=league["id"], name=f"{name} T{i}", owner_user_id=commissioner.id) for i in range(4)]
    db.add_all(teams)
    db.commit()
    return league["id"], [t.id for t in teams]


def run_draft(league_id, expect_snake=None, headers=None):
    resp = client.post("/drafts/start", json={"league_id": league_id}, headers=headers)
    assert resp.status_code == 200, resp.text
    draft_id = resp.json()["id"]
    for _ in range(400):
        if client.post(f"/drafts/{draft_id}/autopick").status_code != 200:
            break
    picks = client.get(f"/drafts/{draft_id}/picks").json()
    assert client.get(f"/drafts/{draft_id}").json()["status"] == "complete"
    return picks


def order_of(picks, rnd):
    return [p["team_id"] for p in picks if p["round"] == rnd]


def roster_count(team_id):
    db.expire_all()
    return db.query(RosterEntry).filter_by(team_id=team_id).count()


def drop_players(team_ids, n):
    for tid in team_ids:
        for e in db.query(RosterEntry).filter_by(team_id=tid).order_by(RosterEntry.id.desc()).limit(n).all():
            db.delete(e)
    db.commit()


# ---- permissions: only the commissioner edits settings, anyone reads ------------------------------------------
lid, tids = make_league("Perm", "dynasty")
assert client.patch(f"/leagues/{lid}/settings", json={"league_format": "keeper"}, headers=H_OTHER).status_code == 403
assert client.patch(f"/leagues/{lid}/settings", json={"league_format": "bogus"}, headers=H).status_code == 422
assert client.get(f"/leagues/{lid}").json()["league_format"] == "dynasty"
assert client.post(f"/leagues/{lid}/rollover", json={}, headers=H_OTHER).status_code == 403
assert client.post(f"/leagues/{lid}/rollover", json={}, headers=H).status_code == 409, "draft not finished"

# ---- Traditional Redraft: rosters reset, random order, snake -------------------------------------------------
lid, tids = make_league("Trad", "traditional_redraft")
first = run_draft(lid)
spots = len(order_of(first, 1)) and max(p["round"] for p in first)
league = client.post(f"/leagues/{lid}/rollover", json={}, headers=H)
assert league.status_code == 200, league.text
league = league.json()
assert (league["season_number"], league["offseason"]) == (2, True), league
assert all(roster_count(t) == 0 for t in tids), "traditional redraft clears rosters"
assert client.post(f"/leagues/{lid}/rollover", json={}, headers=H).status_code == 409, "already in the offseason"
assert client.post("/drafts/start", json={"league_id": lid}).status_code == 401, "offseason draft is commissioner-only"
assert client.get(f"/leagues/{lid}/overview").json()["draft_state"] == "not_started"
second = run_draft(lid, headers=H)
assert sorted(order_of(second, 1)) == sorted(tids) and order_of(second, 2) == order_of(second, 1)[::-1], "random order, snake"
assert client.get(f"/leagues/{lid}").json()["offseason"] is False
seasons = client.get(f"/leagues/{lid}/seasons").json()
assert [s["season_number"] for s in seasons] == [1] and len(seasons[0]["standings"]) == 4
season1 = client.get(f"/leagues/{lid}/seasons/1").json()
assert sum(len(v) for v in season1["data"]["rosters"].values()) == len(first) and len(season1["data"]["drafts"][0]["picks"]) == len(first)
assert client.get(f"/leagues/{lid}/seasons/9").status_code == 404

# ---- Future Draft Pick Redraft: rosters reset, reverse standings, snake ----------------------------------------
lid, tids = make_league("Future", "future_pick_redraft")
first = run_draft(lid)
standing = [r["team_id"] for r in client.post(f"/leagues/{lid}/rollover", json={}, headers=H) and
            client.get(f"/leagues/{lid}/seasons/1").json()["standings"]]
assert all(roster_count(t) == 0 for t in tids)
second = run_draft(lid, headers=H)
assert order_of(second, 1) == standing[::-1], (order_of(second, 1), standing)
assert order_of(second, 2) == standing, "snake: round 2 reverses"

# ---- Keeper and Dynasty: rosters stay, reverse standings, no snake, commissioner sets the rounds --------------
for fmt in ("keeper", "dynasty"):
    lid, tids = make_league(fmt.title(), fmt)
    first = run_draft(lid)
    full = max(p["round"] for p in first)
    assert client.post(f"/leagues/{lid}/rollover", json={}, headers=H).status_code == 409, "rounds are required"
    resp = client.post(f"/leagues/{lid}/rollover", json={"offseason_draft_rounds": 2}, headers=H)
    assert resp.status_code == 200, resp.text
    assert resp.json()["offseason_draft_rounds"] == 2
    standing = [r["team_id"] for r in client.get(f"/leagues/{lid}/seasons/1").json()["standings"]]
    assert all(roster_count(t) == full for t in tids), "keeper/dynasty keep rosters"
    freeze = client.post("/drafts/start", json={"league_id": lid}, headers=H)
    assert freeze.status_code == 409 and "Roster freeze" in freeze.json()["detail"], freeze.text
    drop_players(tids, 2)
    second = run_draft(lid, headers=H)
    assert order_of(second, 1) == standing[::-1] and order_of(second, 2) == standing[::-1], "reverse standings, no snake"
    assert len(second) == 8 and all(roster_count(t) == full for t in tids)
    assert client.get(f"/leagues/{lid}").json()["offseason"] is False

print("Rollover smoke test passed: permissions, all four formats, roster freeze, draft order, archive.")
