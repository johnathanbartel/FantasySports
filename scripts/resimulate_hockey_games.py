"""
Void the simulation of NHL window games that were simulated from a wrong lineup, so the next window-engine sync simulates them again
from the stored feed, on a lineup resolved on the full schedule (src/fantasy_sports/sports/hockey/simulator/lineup_audit.py).

    .\\.venv-api\\Scripts\\python.exe scripts/resimulate_hockey_games.py --league 123            # dry run: only reports
    .\\.venv-api\\Scripts\\python.exe scripts/resimulate_hockey_games.py --league 123 --yes      # does it
    .\\.venv-api\\Scripts\\python.exe scripts/resimulate_hockey_games.py --league 123 --game 456 --yes
    .\.venv-api\Scripts\python.exe scripts/resimulate_hockey_games.py --league 123 --all --keep 456 --yes   # every game with a simulation except #456
    .\.venv-api\Scripts\python.exe scripts/resimulate_hockey_games.py --league 123 --old-model --yes   # also games scored before the value model

DESTRUCTIVE with --yes: each voided game's events and every viewer's watched progress are deleted, the game goes back to scheduled,
and every score, shot and highlight in it can change when it is simulated again. Only games the audit calls "locked, differs"
(frozen on a possibly partial schedule, with periods already published, and different from what the pecking order gives now) are
touched (plus, with --old-model, games whose published periods used the earlier fixed-total scoring); every other game is left alone. Run scripts/audit_hockey_lineups.py first and read it.

The database is whatever DATABASE_URL points at (this script does not read .env; set it yourself). Take a backup first.
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
    parser.add_argument("--game", type=int, action="append", help="only this daily game id (repeatable); default: every flagged game")
    parser.add_argument("--since", help="only windows ending on or after this date (YYYY-MM-DD); default: the last 7 days")
    parser.add_argument("--old-model", action="store_true",
                        help="also void games whose published periods used the earlier fixed-total scoring, so they are simulated by the value model")
    parser.add_argument("--all", action="store_true", dest="everything",
                        help="void EVERY game that has a simulation (in the --since range), not just flagged ones; combine with --keep")
    parser.add_argument("--keep", type=int, action="append", help="with --all: a daily game id to leave exactly as it is (repeatable)")
    parser.add_argument("--yes", action="store_true", help="actually void the games (default is a dry run)")
    args = parser.parse_args(argv)

    from fantasy_sports.platform.db.models.core import League
    from fantasy_sports.platform.db.session import SessionLocal, engine
    from fantasy_sports.sports.hockey.simulator import lineup_audit

    url = engine.url
    print(f"Database: {url.drivername} host={url.host or '(local file)'} name={url.database}")
    with SessionLocal() as db:
        if db.get(League, args.league) is None:
            print(f"No league {args.league}. (Is that a league id and not a game id? scripts/audit_hockey_lineups.py lists the leagues.)")
            return 2
        import datetime
        since = datetime.date.fromisoformat(args.since) if args.since else datetime.date.today() - datetime.timedelta(days=7)
        print(f"Looking at windows ending on or after {since}...", flush=True)
        done = lineup_audit.resimulate_locked(db, args.league, args.game, execute=args.yes, since=since, include_old_model=args.old_model, everything=args.everything, keep=args.keep,
                                              progress=lambda n, total, gid: print(f"  {n}/{total} (game #{gid})", flush=True))
        if args.yes:
            db.commit()
        else:
            db.rollback()

    touched = [d for d in done if d["action"] in ("voided", "would be voided")]
    for d in done:
        print(f"#{d['daily_game_id']}: {d['action']}")
        for side in ("away", "home"):
            s = d["sides"][side]
            if d["action"] in ("voided", "would be voided") and s.get("missing_from_frozen"):
                print(f"    {s['team']} was missing: " + ", ".join(s["missing_from_frozen"]))
    if not touched:
        print("Nothing to do: no game is flagged locked and wrong.")
    elif args.yes:
        print(f"\nVoided {len(touched)} game(s). The next scoreboard or game page load simulates them again from the stored feed.")
    else:
        print(f"\nDRY RUN: {len(touched)} game(s) would be voided. Nothing was changed. Re-run with --yes to do it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
