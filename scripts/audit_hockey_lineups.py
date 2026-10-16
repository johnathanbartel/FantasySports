"""
Read-only audit of every NHL window game in one league: the lineup the simulator froze against what each team's pecking order
gives on the schedule stored now (src/fantasy_sports/sports/hockey/simulator/lineup_audit.py).

    .\\.venv-api\\Scripts\\python.exe scripts/audit_hockey_lineups.py --league 123

The database is whatever DATABASE_URL points at (this script does not read .env; set it yourself). On PostgreSQL the transaction is
opened READ ONLY, so nothing can be written. Statuses: ok / not started / repairs on sync / locked, differs / frozen, differs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--league", type=int, required=True, help="league id (not a game id)")
    args = parser.parse_args(argv)

    from sqlalchemy import text

    from fantasy_sports.platform.db.models.core import League, LeagueSeries, SeriesDailyGame
    from fantasy_sports.platform.db.session import SessionLocal, engine
    from fantasy_sports.sports.hockey.simulator import lineup_audit

    url = engine.url
    print(f"Database: {url.drivername} host={url.host or '(local file)'} name={url.database}")
    with SessionLocal() as db:
        if engine.dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        if db.get(League, args.league) is None:
            print(f"No league {args.league}. Leagues with NHL window games:")
            rows = (db.query(League.id, League.name).join(LeagueSeries, LeagueSeries.league_id == League.id)
                    .join(SeriesDailyGame, SeriesDailyGame.series_id == LeagueSeries.id)
                    .filter(SeriesDailyGame.window_index.isnot(None)).distinct().order_by(League.id).all())
            for league_id, name in rows:
                print(f"  {league_id}: {name}")
            return 2
        games = lineup_audit.lineup_audit(db, args.league)
        db.rollback()

    counts: dict[str, int] = {}
    for g in games:
        counts[g["status"]] = counts.get(g["status"], 0) + 1
    print(f"League {args.league}: {len(games)} window games - " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    for g in games:
        print(f"\n#{g['daily_game_id']}  window {g['window_index']}  {g['start']}..{g['end']}  {g['game_status']}  "
              f"published {g['published_periods']}/3  [{g['status']}]  ({g['stored_games_in_window']} real games stored)")
        for side in ("away", "home"):
            s = g["sides"][side]
            line = f"    {s['team']}: {s['frozen'] if s['frozen'] is not None else '-'} frozen / {s['would_start_now']} would start now"
            if s.get("missing_from_frozen"):
                line += "  MISSING: " + ", ".join(s["missing_from_frozen"])
            if s.get("only_in_frozen"):
                line += "  only frozen: " + ", ".join(s["only_in_frozen"])
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
