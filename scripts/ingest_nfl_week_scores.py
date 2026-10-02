"""Score one real NFL regular-season week into the app database.

Downloads nflfastR play-by-play, the schedule and the season roster from
nflverse (nfl_data_py, no key), runs every play through the NFL attribution
pipeline (offense, kicker, and defense by individual / DL-LB-DB unit / whole
team) and stores play_credits and weekly_player_scores. See
src/fantasy_sports/sports/football/professional/ingest_scores.py.

Run the roster ingest first so the teams and assets exist, from the repository
root with the data-pipeline environment:

    $env:DATABASE_URL = "sqlite:///./user_run.db"
    .venv/Scripts/python.exe scripts/ingest_nfl_rosters_and_stats.py --season 2024 --week 1
    .venv/Scripts/python.exe scripts/ingest_nfl_week_scores.py --season 2024 --week 1

Re-running a week replaces it.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DATABASE_URL", "sqlite:///./user_run.db")

import nfl_data_py as nfl

from fantasy_sports.platform.db.session import DATABASE_URL, SessionLocal
from fantasy_sports.sports.football.professional.defense import (
    DefenseBaseline,
    EventWeights,
    PositionResolver,
)
from fantasy_sports.sports.football.professional.ingest_scores import score_week


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=int, default=2024)
    parser.add_argument("--week", type=int, default=1)
    args = parser.parse_args()

    pbp = nfl.import_pbp_data([args.season], downcast=False)
    pbp = pbp[(pbp.week == args.week) & (pbp.season_type == "REG") & pbp.play_type.isin(("run", "pass", "field_goal"))]
    sched = nfl.import_schedules([args.season])
    sched = sched[(sched.week == args.week) & (sched.game_type == "REG")]
    roster = nfl.import_seasonal_rosters([args.season]).dropna(subset=["player_id", "position"])
    resolver = PositionResolver.from_roster_frame(roster)

    print(f"Database: {DATABASE_URL}")
    with SessionLocal() as db:
        summary = score_week(db, args.season, args.week, sched, pbp, resolver,
                             EventWeights.load(), DefenseBaseline.load())
        db.commit()
    unmatched = summary.pop("unmatched_ids")
    print(summary)
    worst = sorted(unmatched.items(), key=lambda kv: -kv[1])[:5]
    print("Largest unmatched credit (id -> |EPA|):", worst)


if __name__ == "__main__":
    main()
