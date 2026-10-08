"""Add the NFL universe and its v1 roster slots to the configured database.

This is reference-data setup only. It assumes the database schema is already
migrated, makes no external requests, and is safe to run repeatedly. Set
DATABASE_URL explicitly before running it against the intended database:

    $env:DATABASE_URL = "sqlite:///./user_run.db"
    .venv-api/Scripts/python.exe scripts/seed_nfl_universe.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fantasy_sports.platform.db.session import DATABASE_URL, SessionLocal
from fantasy_sports.platform.nfl_reference import ROSTER_SLOTS, seed_nfl_reference_data  # noqa: F401


def main() -> None:
    with SessionLocal() as db:
        nfl, inserted = seed_nfl_reference_data(db)
        print(
            f"NFL universe id={nfl.id} is ready on {DATABASE_URL}; "
            f"inserted {inserted} roster slot definition(s)."
        )


if __name__ == "__main__":
    main()
