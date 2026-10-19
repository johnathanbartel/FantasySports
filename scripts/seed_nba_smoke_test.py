"""
NBA stand-up on the shared platform, through the real HTTP API (FastAPI TestClient): the "nba" universe and its
15-player roster slots are seeded on demand by league creation, an NBA league plays the same window / series
calendar as hockey (series length, series schedule, window locks), and a short snake draft round-trips.

Checks: reference data seeded on demand and idempotent (5 starters PG SG SF PF C + 10 bench = 15), 15 draft rounds by
default and a different count refused, series_length_days accepted for NBA (default 7; 5 / 7 / 10 only) and still
refused for NFL, hockey-style standings/playoff settings accepted, the series schedule generated for an NBA league,
the window sport set (NHL + NBA) and the window lock, a draft pick, and that an NHL league still works beside it.

Real NBA players and schedules are not ingested here (that needs external data); the players are throwaway rows.
Uses its own throwaway SQLite file (nba_smoke_test.db). Run from the repo root with the API venv:

    .venv-api/Scripts/python.exe scripts/seed_nba_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

db_path = Path(os.environ.get("NBA_SMOKE_DB", "nba_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import auto_schedule, game_lock, series_schedule
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, League, LeagueSeries, Player, RealTeam, RosterSlotDefinition, Team, Universe, User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.nba_reference import ROSTER_SIZE, seed_nba_reference_data
from fantasy_sports.platform.window_sports import WINDOW_SPORTS, is_window_sport, window_universe_code

Base.metadata.create_all(bind=engine)
client = TestClient(app)


def main():
    db = SessionLocal()
    commissioner = User(email="nba@example.com", display_name="Commissioner")
    db.add(commissioner)
    db.commit()
    resp = client.post("/sessions", json={"user_id": commissioner.id})
    assert resp.status_code == 200, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['token']}"}

    def create(**extra):
        body = {"name": "NBA League", "commissioner_user_id": commissioner.id, "universe_codes": ["nba"], "size": 2}
        body.update(extra)
        return client.post("/leagues", json=body, headers=headers)

    # --- reference data is seeded on demand -----------------------------------
    assert db.query(Universe).filter_by(code="nba").first() is None
    assert is_window_sport("nba") and is_window_sport("nhl") and not is_window_sport("nfl")
    assert WINDOW_SPORTS == {"nhl", "nba"}

    resp = create()
    assert resp.status_code == 200, resp.text
    league = resp.json()
    nba = db.query(Universe).filter_by(code="nba").one()
    assert (nba.sport, nba.level) == ("basketball", "pro")
    slots = {s.slot_code: (s.slot_count, s.is_starter) for s in db.query(RosterSlotDefinition).filter_by(universe_id=nba.id)}
    assert slots == {"PG": (1, True), "SG": (1, True), "SF": (1, True), "PF": (1, True), "C": (1, True), "BENCH": (10, False)}, slots
    assert ROSTER_SIZE == 15 and sum(c for c, _ in slots.values()) == 15
    assert league["draft_rounds"] == 15, league
    assert league["series_length_days"] == 7, league
    print("Created NBA league on a fresh database: universe + 15 roster slots (5 starters + 10 bench), 15 draft rounds, 7-day series.")

    _, inserted = seed_nba_reference_data(db)
    assert inserted == 0 and db.query(RosterSlotDefinition).filter_by(universe_id=nba.id).count() == 6, "seeding is idempotent"

    # --- validation -----------------------------------------------------------
    assert create(draft_rounds=13).status_code == 400, "draft rounds must equal the 15-player roster"
    assert create(draft_rounds=15).status_code == 200
    assert create(series_length_days=4).status_code == 400
    for days in (5, 10):
        r = create(series_length_days=days)
        assert r.status_code == 200 and r.json()["series_length_days"] == days, r.text
    r = client.post("/leagues", json={"name": "NFL", "commissioner_user_id": commissioner.id, "universe_codes": ["nfl"],
                                      "size": 2, "series_length_days": 7}, headers=headers)
    assert r.status_code == 400, "series length is still refused outside the window sports"
    r = create(standings_mode="points", points_per_win=2, playoff_teams=2)
    assert r.status_code == 200, r.text
    assert r.json()["playoff_teams"] == 2
    print("Validation passed: draft rounds, series length 5/7/10 only, NFL still refused, standings settings accepted.")

    # --- window / series schedule ---------------------------------------------
    lg = db.get(League, league["id"])
    assert window_universe_code(db, lg.id) == "nba"
    assert series_schedule.league_uses_windows(db, lg.id) and not series_schedule.league_is_nhl(db, lg.id)
    db.add_all([Team(league_id=lg.id, name="Team A", owner_user_id=commissioner.id),
                Team(league_id=lg.id, name="Team B", owner_user_id=commissioner.id)])
    db.commit()
    start = auto_schedule.next_window_start(datetime.date(2026, 10, 21))
    series_schedule.generate_schedule(db, lg, start, 1)
    db.commit()
    assert db.query(LeagueSeries).filter_by(league_id=lg.id).count() > 0
    assert auto_schedule.has_schedule(db, lg)
    assert auto_schedule.calculate_nhl_series_start(db, None, datetime.date(2026, 10, 21), "nba") >= datetime.date(2026, 10, 21)
    print("Series schedule generated for an NBA league; auto-schedule treats it as a window league.")

    # --- window lock: an NBA game that has started locks its team for the window ----
    from fantasy_sports.platform.db.models.core import Game, Week
    week = Week(universe_id=nba.id, season_year=2026, week_number=1,
                start_date=datetime.date(2026, 10, 18), end_date=datetime.date(2026, 10, 24))
    db.add(week)
    db.flush()
    team_x = RealTeam(universe_id=nba.id, name="Boston Celtics", external_ref="BOS")
    team_y = RealTeam(universe_id=nba.id, name="Miami Heat", external_ref="MIA")
    db.add_all([team_x, team_y])
    db.flush()
    stars = [Player(universe_id=nba.id, real_team_id=team_x.id, full_name="Jayson Tatum", position="SF"),
             Player(universe_id=nba.id, real_team_id=team_y.id, full_name="Bam Adebayo", position="C")]
    for i in range(40):
        stars.append(Player(universe_id=nba.id, real_team_id=team_y.id, full_name=f"Depth Wing {i:02d}", position="SG"))
    db.add_all(stars)
    db.flush()
    assets = [DraftableAsset(universe_id=nba.id, asset_type="player", player_id=p.id) for p in stars]
    db.add_all(assets)
    db.flush()
    now = datetime.datetime(2026, 10, 21, 23, 0)                 # Wed 7pm Eastern, naive UTC
    db.add(Game(universe_id=nba.id, week_id=week.id, home_team_id=team_x.id, away_team_id=team_y.id,
                kickoff_at=datetime.datetime(2026, 10, 21, 19, 0)))   # Eastern wall clock, like the NHL rows
    db.commit()
    locked = game_lock.locked_assets(db, lg.id, now)
    assert {a.id for a in assets} <= locked, "players of both teams are locked once the game has started"
    assert game_lock.locked_assets(db, lg.id, datetime.datetime(2026, 10, 21, 20, 0)) == set(), "not before tip-off"
    assert game_lock._period_word(db, assets[0].id) == "window"
    print("Window lock passed: an NBA game locks both teams' players for the window, not before tip-off.")

    # --- draft ------------------------------------------------------------------
    resp = client.post("/drafts/start", json={"league_id": lg.id})
    assert resp.status_code == 200, resp.text
    draft = resp.json()
    resp = client.post(f"/drafts/{draft['id']}/pick", json={"draftable_asset_id": assets[0].id}, headers=headers)
    assert resp.status_code == 200, resp.text
    print(f"Draft pick: {resp.json()['display_name']}")

    # --- NHL beside it ------------------------------------------------------------
    r = client.post("/leagues", json={"name": "NHL", "commissioner_user_id": commissioner.id, "universe_codes": ["nhl"],
                                      "size": 2}, headers=headers)
    assert r.status_code == 200 and r.json()["draft_rounds"] == 18 and r.json()["series_length_days"] == 7, r.text
    nhl_lg = db.get(League, r.json()["id"])
    assert series_schedule.league_is_nhl(db, nhl_lg.id) and window_universe_code(db, nhl_lg.id) == "nhl"
    assert window_universe_code(db, lg.id) == "nba"
    print("NHL league still creates alongside NBA (18 rounds, 7-day series).")

    print("\nNBA stand-up smoke test passed.")


if __name__ == "__main__":
    main()
