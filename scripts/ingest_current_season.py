"""
Puts CURRENT players in the app: this season's FBS teams and rosters, the
season's regular-season weeks, and last completed season's ESPN-standard points
for the players (see sports/football/college/ingest_current.py).

    DATABASE_URL=sqlite:///./user_run.db .venv/Scripts/python.exe scripts/ingest_current_season.py --season 2026 --points-season 2025

Uses .venv (it has the cfbd package). Reads CFBD_API_KEY from the environment or
the repo's .env. Idempotent - safe to re-run to pick up roster changes. It spends
about 20 CFBD calls (one for all rosters, one calendar, one per conference).
"""

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DATABASE_URL", "sqlite:///./user_run.db")

import cfbd

from fantasy_sports.platform.db.models import core  # noqa: F401
from fantasy_sports.platform.db.session import SessionLocal
from fantasy_sports.sports.football.college import ingest_current as ingest
from fantasy_sports.sports.football.models import PlayerRecruitRating, PlayerSeasonPoints


def api_key() -> str:
    key = os.environ.get("CFBD_API_KEY")
    if not key and (ROOT / ".env").exists():
        m = re.search(r"^CFBD_API_KEY=(.*)$", (ROOT / ".env").read_text(), re.M)
        key = m.group(1).strip() if m else None
    if not key:
        sys.exit("CFBD_API_KEY not set (environment or .env) - see .env.example")
    return key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=int, required=True, help="the current season, e.g. 2026")
    parser.add_argument("--points-season", type=int, required=True, help="last completed season, e.g. 2025")
    args = parser.parse_args()

    from fantasy_sports.platform.db.session import engine
    PlayerSeasonPoints.__table__.create(bind=engine, checkfirst=True)
    PlayerRecruitRating.__table__.create(bind=engine, checkfirst=True)
    db = SessionLocal()
    client = cfbd.ApiClient(cfbd.Configuration(access_token=api_key()))
    print(f"Database: {os.environ['DATABASE_URL']}")

    r = ingest.sync_teams_and_rosters(db, client, args.season)
    print(f"{args.season} rosters: {r['fbs_teams']} FBS teams ({r['new_teams']} new to the app); "
          f"{r['players_current']} current players ({r['new_players']} new, {r['changed_team']} changed team); "
          f"{r['retired']} earlier players no longer on an FBS roster marked inactive; "
          f"{r['new_defense_groups']} new defense groups")
    rr = ingest.sync_recruit_ratings(db, client, args.season, r["roster_info"])
    print(f"Recruiting ratings: {rr['rated']} of {rr['players']} current players have a high-school recruiting record")
    added = ingest.sync_weeks(db, client, args.season)
    print(f"{args.season} calendar: {added} regular-season week(s) added")
    p = ingest.sync_season_points(db, client, args.points_season)
    print(f"{args.points_season} stats ({p['conferences']} conferences, {p['stat_rows']} stat rows): "
          f"{p['active_with_points']} of {p['active_players']} current players have a {args.points_season} stat line")
    db.commit()


if __name__ == "__main__":
    main()
