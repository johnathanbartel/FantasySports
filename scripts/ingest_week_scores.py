"""
Scores real games for the current season's weeks (games + plays + per-play credit +
weekly player scores) - see sports/football/college/ingest_scores.py.

    DATABASE_URL=sqlite:///./user_run.db .venv/Scripts/python.exe scripts/ingest_week_scores.py --season 2026 --weeks 1 2 3 --first-game-date 2026-09-03

Uses .venv (cfbd). Needs scripts/ingest_current_season.py to have been run for the season
(teams, players, Week rows). About 4 CFBD calls per week plus 3 for the defense context.
Games before --first-game-date are ignored. Re-running a week replaces its scores.
"""

import argparse
import datetime
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
from fantasy_sports.sports.football.college import ingest_scores


def api_key() -> str:
    key = os.environ.get("CFBD_API_KEY")
    if not key and (ROOT / ".env").exists():
        m = re.search(r"^CFBD_API_KEY=(.*)$", (ROOT / ".env").read_text(), re.M)
        key = m.group(1).strip() if m else None
    if not key:
        sys.exit("CFBD_API_KEY not set (environment or .env)")
    return key


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--weeks", type=int, nargs="+", required=True)
    ap.add_argument("--first-game-date", type=datetime.date.fromisoformat, required=True)
    args = ap.parse_args()
    db = SessionLocal()
    client = cfbd.ApiClient(cfbd.Configuration(access_token=api_key()))
    print(f"Database: {os.environ['DATABASE_URL']}")
    s = ingest_scores.score_weeks(db, client, args.season, args.weeks, args.first_game_date)
    db.commit()
    for w, r in s["weeks"].items():
        print(f"week {w}: {r['games']} games, {r['plays']} plays, {r['credits']} credits -> {r['scored']} players/groups scored")
    total = s["credit_total"] or 1.0
    print(f"credit that couldn't be tied to a current player: {100 * s['credit_unmatched'] / total:.1f}% of absolute credit")
    print(f"  of which an opponent outside the FBS (no assets exist for it): {100 * s['unmatched_non_fbs_team'] / total:.1f}%")
    name_miss = sum(s["unmatched_fbs_names"].values())
    print(f"  of which an FBS team's player we couldn't match by name: {100 * name_miss / total:.1f}%")
    for (name, team), pts in sorted(s["unmatched_fbs_names"].items(), key=lambda kv: -kv[1])[:12]:
        print(f"    {name} ({team}): {pts:.1f}")


if __name__ == "__main__":
    main()
