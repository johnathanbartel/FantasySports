"""Moves NHL leagues from one game per calendar day to one game per fantasy window (docs/hockey_spec.md section 5).

For every NHL league in the database that DATABASE_URL points at (or one league with --league): each window whose days
have not been played collapses into a single window row and its simulation is discarded, so it is scored again as one
game and published period by period. A window with any FINAL day is left exactly as played. Safe to re-run.

Dry run by default (rolls back and prints what would change); pass --yes to apply. Run `alembic upgrade head` first.

    .venv-api/Scripts/python.exe scripts/convert_to_window_rows.py [--league ID] [--yes]
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--league", type=int, help="convert only this league id")
    parser.add_argument("--yes", action="store_true", help="apply the changes (default: dry run)")
    args = parser.parse_args()

    from fantasy_sports.platform import series_schedule
    from fantasy_sports.platform.db.models.core import League, LeagueSeries
    from fantasy_sports.platform.db.session import SessionLocal

    with SessionLocal() as db:
        ids = [args.league] if args.league else [
            r[0] for r in db.query(LeagueSeries.league_id).distinct().order_by(LeagueSeries.league_id)]
        total_converted = total_kept = 0
        for league_id in ids:
            league = db.get(League, league_id)
            if league is None or not series_schedule.league_is_nhl(db, league_id):
                continue
            result = series_schedule.convert_to_window_rows(db, league_id)
            total_converted += result["converted_windows"]
            total_kept += result["left_as_played"]
            print(f"league {league_id} ({league.name}): {result['converted_windows']} windows converted, "
                  f"{result['left_as_played']} left as played")
        if args.yes:
            db.commit()
            print(f"applied: {total_converted} windows converted, {total_kept} left as played")
        else:
            db.rollback()
            print(f"dry run (nothing changed): {total_converted} windows would convert, {total_kept} left as played. Pass --yes to apply.")


if __name__ == "__main__":
    main()
