"""
Deletes every league - and everything that hangs off a league - from one or
more SQLite database files. Players, teams' real-world data, games, plays,
scores, weeks, users and premium entitlements are left alone.

Dev tool. It is destructive, so it does a dry run (counts only) unless you pass
--yes, and with --yes it first copies each database to backups/ so the deletion
can be undone by copying the file back.

    .venv-api/Scripts/python.exe scripts/delete_all_leagues.py user_run.db fantasy.db          # dry run
    .venv-api/Scripts/python.exe scripts/delete_all_leagues.py user_run.db fantasy.db --yes    # do it
"""

import argparse
import datetime
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (table, WHERE clause over the ids collected below) - children before parents.
# {leagues} {teams} {rdrafts} {adrafts} {drafts} are comma-separated id lists.
STEPS = [
    ("recruiting_result_acks", "draft_id IN ({rdrafts})"),
    ("recruiting_bid_results", "draft_id IN ({rdrafts})"),
    ("recruiting_charges", "draft_id IN ({rdrafts})"),
    ("recruiting_awards", "draft_id IN ({rdrafts})"),
    ("recruiting_bids", "draft_id IN ({rdrafts})"),
    ("recruiting_thresholds", "draft_id IN ({rdrafts})"),
    ("recruiting_drafts", "league_id IN ({leagues})"),
    ("recruiting_settings", "league_id IN ({leagues})"),
    ("free_agency_transactions", "league_id IN ({leagues})"),
    ("auction_bids", "auction_round_id IN (SELECT id FROM auction_rounds WHERE auction_draft_id IN ({adrafts}))"),
    ("auction_rounds", "auction_draft_id IN ({adrafts})"),
    ("auction_team_budgets", "auction_draft_id IN ({adrafts})"),
    ("auction_drafts", "league_id IN ({leagues})"),
    ("draft_picks", "draft_id IN ({drafts})"),
    ("trade_votes", "trade_id IN (SELECT id FROM trades WHERE league_id IN ({leagues}))"),
    ("trade_items", "trade_id IN (SELECT id FROM trades WHERE league_id IN ({leagues}))"),
    ("trades", "league_id IN ({leagues})"),
    ("weekly_matchups", "league_id IN ({leagues})"),
    ("weekly_team_scores", "team_id IN ({teams})"),
    ("lineup_entries", "team_id IN ({teams})"),
    ("roster_entries", "team_id IN ({teams})"),
    ("teams", "league_id IN ({leagues})"),
    ("divisions", "league_id IN ({leagues})"),
    ("conferences", "league_id IN ({leagues})"),
    ("league_universes", "league_id IN ({leagues})"),
    ("leagues", "id IN ({leagues})"),
]


def ids(con, table, where="1=1"):
    try:
        return [r[0] for r in con.execute(f"SELECT id FROM {table} WHERE {where}")]
    except sqlite3.OperationalError:
        return []          # an older database that predates this table


def has_table(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("databases", nargs="+")
    parser.add_argument("--yes", action="store_true", help="actually delete (default: dry run)")
    args = parser.parse_args()

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    for name in args.databases:
        path = (ROOT / name).resolve()
        if not path.exists():
            sys.exit(f"{name}: no such file")
        con = sqlite3.connect(path)
        leagues = ids(con, "leagues")
        print(f"\n{name}: {len(leagues)} league(s): "
              f"{[(r[0], r[1]) for r in con.execute('SELECT id, name FROM leagues ORDER BY id')]}")
        if not leagues:
            continue
        fmt = {
            "leagues": ",".join(map(str, leagues)) or "NULL",
            "teams": ",".join(map(str, ids(con, "teams", f"league_id IN ({','.join(map(str, leagues))})"))) or "NULL",
            "rdrafts": ",".join(map(str, ids(con, "recruiting_drafts", f"league_id IN ({','.join(map(str, leagues))})"))) or "NULL",
            "adrafts": ",".join(map(str, ids(con, "auction_drafts", f"league_id IN ({','.join(map(str, leagues))})"))) or "NULL",
            "drafts": ",".join(map(str, ids(con, "drafts", f"league_id IN ({','.join(map(str, leagues))})"))) or "NULL",
        }
        counts = {}
        for table, where in STEPS:
            if has_table(con, table):
                counts[table] = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where.format(**fmt)}").fetchone()[0]
        print("  would delete: " + ", ".join(f"{t} {n}" for t, n in counts.items() if n))
        if not args.yes:
            continue
        backup_dir = ROOT / "backups"
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / f"{path.stem}-{stamp}.db"
        con.close()
        shutil.copyfile(path, backup)
        print(f"  backed up to {backup.relative_to(ROOT)}")
        con = sqlite3.connect(path)
        con.execute("PRAGMA foreign_keys = OFF")
        for table, where in STEPS:
            if has_table(con, table):
                con.execute(f"DELETE FROM {table} WHERE {where.format(**fmt)}")
        con.commit()
        left = con.execute("SELECT COUNT(*) FROM leagues").fetchone()[0]
        print(f"  deleted. leagues left: {left}")
        con.close()
    if not args.yes:
        print("\nDry run only - nothing was changed. Re-run with --yes to delete.")


if __name__ == "__main__":
    main()
