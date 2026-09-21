"""
Pulls each seeded player's full-season CFBD stat line and stores their fantasy
points under ESPN standard scoring (sports/football/college/season_scoring.py)
in player_season_points, so the recruiting board can show and sort by last
season's points.

One CFBD call per real team that has seeded players. Idempotent: re-running
replaces that season's rows. Defense groups get no row (no position-group
scoring in ESPN standard).

Usage (repo root, .venv - it has the cfbd package):
    DATABASE_URL=sqlite:///./mock_draft_demo.db .venv/Scripts/python.exe scripts/ingest_season_points.py [--season 2024]
Reads CFBD_API_KEY from the environment or the repo's .env file.
"""

import argparse
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DATABASE_URL", "sqlite:///./mock_draft_demo.db")

import cfbd

from fantasy_sports.platform.db.models import core  # noqa: F401
from fantasy_sports.platform.db.models.core import Player, RealTeam
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.sports.football.college.season_scoring import SCORING_SYSTEM, espn_standard_points
from fantasy_sports.sports.football.models import PlayerSeasonPoints


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
    parser.add_argument("--season", type=int, default=2024)
    args = parser.parse_args()

    # Only this script's own table - create_all would also create every other (possibly newer,
    # differently-shaped) table in a DB that was seeded before those existed.
    PlayerSeasonPoints.__table__.create(bind=engine, checkfirst=True)
    db = SessionLocal()
    stats_api = cfbd.StatsApi(cfbd.ApiClient(cfbd.Configuration(access_token=api_key())))

    players_by_ref = {p.external_ref: p for p in db.query(Player).filter(Player.external_ref.isnot(None))}
    team_ids = {p.real_team_id for p in players_by_ref.values() if p.real_team_id}
    db.query(PlayerSeasonPoints).filter_by(season=args.season, scoring_system=SCORING_SYSTEM).delete()

    matched = 0
    for team in db.query(RealTeam).filter(RealTeam.id.in_(team_ids)).order_by(RealTeam.id):
        for attempt in range(4):
            try:
                rows = stats_api.get_player_season_stats(year=args.season, team=team.name)
                break
            except cfbd.exceptions.ApiException as e:
                if attempt == 3:
                    raise
                print(f"  {team.name}: {e.status}, retrying"); time.sleep(3 * (attempt + 1))
        lines = defaultdict(dict)
        for r in rows:
            try:
                lines[r.player_id][(r.category, r.stat_type)] = float(str(r.stat).replace(",", ""))
            except ValueError:
                pass  # non-numeric (e.g. a 'LONG' with a marker) - none of the scored stats are like that
        got = 0
        for ref, stat_line in lines.items():
            player = players_by_ref.get(str(ref))
            if player is None:
                continue
            points, breakdown = espn_standard_points(stat_line)
            db.add(PlayerSeasonPoints(player_id=player.id, season=args.season, scoring_system=SCORING_SYSTEM,
                                      points=points, breakdown=breakdown))
            got += 1
        matched += got
        print(f"{team.name:<18} {got:>3} of its seeded players have a {args.season} stat line")
    db.commit()

    total = len(players_by_ref)
    print(f"\n{matched} of {total} seeded players matched ({total - matched} had no {args.season} stats - "
          f"e.g. freshmen who didn't play, or an id mismatch).")


if __name__ == "__main__":
    main()
