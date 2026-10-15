"""
Pulls the current NHL injury report from ESPN's free public endpoint and updates every NHL player's injury status in
the database `DATABASE_URL` points at (see platform/injury_sync.py). Prints what changed. Safe to re-run; a failed or
implausibly short response changes nothing. No API key needed. Run from the repo root with the API venv:

    .venv-api/Scripts/python.exe scripts/ingest_nhl_injuries.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fantasy_sports.platform.db.session import SessionLocal
from fantasy_sports.platform import injury_sync


def main():
    db = SessionLocal()
    try:
        changes = injury_sync.sync(db)
    finally:
        db.close()
    if changes is None:
        print("Could not fetch the ESPN injury report; nothing changed.")
        return 1
    for c in changes:
        print(f"{c.name}: {c.old_status or 'healthy'} -> {c.new_status or 'healthy'}{f' ({c.note})' if c.note else ''}")
    print(f"{len(changes)} change(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
