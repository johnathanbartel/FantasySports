"""
Adds the "nhl" universe and its product-brief roster slot definitions to whatever database
DATABASE_URL points at, so NHL leagues can be created from the same
running server that already hosts CFB leagues - no separate NHL site or
process. Idempotent and additive-only: does nothing if an "nhl" universe
already exists, and never touches any other row or table.

Run against the live site's database (back it up first - this writes to a
real *.db file):

    $env:DATABASE_URL = "sqlite:///./user_run.db"
    .venv-api/Scripts/python.exe scripts/seed_hockey_universe.py

No real teams or players are ingested by this script - only the universe
and the C, W, W, D, D, G starting structure plus bench. See README.md's Data pipeline section for the
still-undecided NHL data source.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.hockey_reference import ROSTER_SLOTS, seed_hockey_reference_data
from fantasy_sports.platform.db.session import DATABASE_URL, SessionLocal, engine

# Registers shots/shot_credits on Base.metadata so create_all below can add
# them if this database predates the hockey stand-up.
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

def main():
    Base.metadata.create_all(bind=engine)  # no-op for tables that already exist
    db = SessionLocal()
    nhl, inserted = seed_hockey_reference_data(db)
    print(f"NHL universe id={nhl.id} is ready on {DATABASE_URL}; "
          f"inserted {inserted} of {len(ROSTER_SLOTS)} roster slot definitions.")


if __name__ == "__main__":
    main()
