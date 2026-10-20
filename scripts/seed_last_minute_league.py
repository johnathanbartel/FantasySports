"""
Builds the owner's "Last Minute League" (10-team NBA keeper league, ESPN league 395243481, settings read 2026-10-19) in a
THROWAWAY database: NBA league through the real create-league API, its own 22 + 3 IR roster layout, and the ESPN scoring
formula imported and frozen (FS-037).  Settings the platform can't express yet print a SKIP line (see
docs/last_minute_league_gap_audit.md).  Rookie-draft rules (held on Sleeper) are TBD and left at the defaults.

    .venv-api/Scripts/python.exe scripts/seed_last_minute_league.py
"""

import datetime
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

db_path = Path(os.environ.get("LML_DB", "last_minute_league_seed.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import imported_scoring, lineups
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import League, Team, User
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.nba_reference import apply_league_layout

ESPN_SCORING_ITEMS = [          # ESPN stat id -> points
    {"stat_id": 0, "points": 0.5}, {"stat_id": 17, "points": 0.5}, {"stat_id": 6, "points": 1.0},
    {"stat_id": 3, "points": 1.0}, {"stat_id": 2, "points": 2.0}, {"stat_id": 1, "points": 2.0},
    {"stat_id": 11, "points": -1.0}, {"stat_id": 37, "points": 1.0}, {"stat_id": 38, "points": 2.0},
]
# 10 starters (PG SG SF PF C G F + 3 UTIL), 12 bench, 3 IR; IR is not one of the 22 roster spots.
LAYOUT = dict(g=1, f=1, util=3, bench=12, ir=3)

SKIPS = [
    "lineup lock per player at his own game start (INDIVIDUAL_GAME): platform locks a whole window at the first game (game_lock.py)",
    "roster lock at the first game of the scoring period: not modelled",
    "undroppable list: no such setting",
    "1/1/2-week playoff round lengths: not configurable (playoffs.py only picks the field and byes)",
    "(partly) 17 one-week regular-season matchups: the series schedule is a round robin (9 series for 10 teams), not a set week count",
    "playoff seeding by total points scored, no reseeding: seeding is by standings order with tiebreakers only",
    "matchup tie broken by most 3PM (a stat tiebreak): tiebreakers.py has fixed keys, none per stat",
    "waiverOrderReset false (a rolling waiver order): the order resets weekly",
    "(partly) veto threshold of a fixed 5 votes: the platform uses a majority of the uninvolved person-managed teams (8 -> 5 here)",
    "(partly) ESPN offline initial draft with a hand-set order: use draft_order_mode='manual' / the offline draft tools on the platform instead",
]


def main():
    Base.metadata.create_all(bind=engine)
    client = TestClient(app)
    db = SessionLocal()
    commissioner = User(email="lml@example.com", display_name="Commissioner")
    db.add(commissioner)
    db.commit()
    token = client.post("/sessions", json={"user_id": commissioner.id}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.post("/leagues", headers=headers, json={
        "name": "Last Minute League", "commissioner_user_id": commissioner.id, "universe_codes": ["nba"], "size": 10,
        "league_format": "keeper", "keeper_limit": 22, "keeper_draft_style": "rookie_resign",
        "acquisition_type": "waivers", "waiver_hours": 24, "waiver_processing_days": [6],
        "acquisition_limit": 7, "acquisition_limit_period": "series",
        "series_length_days": 7, "playoff_teams": 6, "playoff_consolation": True,
        "trade_deadline": datetime.datetime(2027, 2, 15, 17, 0).isoformat(),
    })
    assert resp.status_code == 200, resp.text
    league = db.get(League, resp.json()["id"])
    league.trade_review_hours, league.trade_review_mode = 24, "league"
    db.add_all(Team(league_id=league.id, name=f"Team {i + 1}", owner_user_id=commissioner.id) for i in range(10))
    apply_league_layout(db, league, **LAYOUT)
    imported_scoring.create_formula(db, league, ESPN_SCORING_ITEMS, "espn")
    db.commit()

    slots = lineups.league_slots(db, league.id)
    starters = sum(s.slot_count for s in slots if s.is_starter)
    bench = sum(s.slot_count for s in slots if s.slot_code == "BENCH")
    ir = sum(s.slot_count for s in slots if s.slot_code == "IR")
    assert (starters, bench, ir) == (10, 12, 3) and league.draft_rounds == 22
    print(f"League {league.id} '{league.name}': {league.size} teams, {league.league_format}, scoring_system={league.scoring_system}")
    print(f"Layout: {starters} starters / {bench} bench / {ir} IR ({starters + bench} roster spots) = "
          + ", ".join(f"{s.slot_code} x{s.slot_count}" for s in slots))
    print("Imported ESPN formula (locked): " + ", ".join(f"{k} {v:+g}" for k, v in imported_scoring.formula(db, league.id).items()))
    print(f"Waivers: {league.acquisition_type}, {league.waiver_hours}h, process weekday {league.waiver_processing_days}; "
          f"acquisition limit {league.acquisition_limit}/{league.acquisition_limit_period}; "
          f"keepers {league.keeper_limit}; playoffs {league.playoff_teams} teams + consolation; trade deadline {league.trade_deadline}")
    print("Rookie draft (Sleeper): rounds, order and pick trading are TBD; left at platform defaults "
          f"(offseason_draft_rounds={league.offseason_draft_rounds}, pick_trading_enabled={league.pick_trading_enabled}).")
    for line in SKIPS:
        print("SKIP (partly): " + line[8:].lstrip() if line.startswith("(partly)") else "SKIP (missing): " + line)
    db.close()
    engine.dispose()
    db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
