"""
Adds the "nhl" universe and its roster slot definitions (see
src/fantasy_sports/sports/hockey/README.md) to whatever database
DATABASE_URL points at, so NHL leagues can be created from the same
running server that already hosts CFB leagues - no separate NHL site or
process. Idempotent and additive-only: does nothing if an "nhl" universe
already exists, and never touches any other row or table.

Run against the live site's database (back it up first - this writes to a
real *.db file):

    $env:DATABASE_URL = "sqlite:///./user_run.db"
    .venv-api/Scripts/python.exe scripts/seed_hockey_universe.py

No real teams or players are ingested by this script - only the universe
and roster structure. See README.md's Data pipeline section for the
still-undecided NHL data source.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import RosterSlotDefinition, Universe
from fantasy_sports.platform.db.session import DATABASE_URL, SessionLocal, engine

# Registers shots/shot_credits on Base.metadata so create_all below can add
# them if this database predates the hockey stand-up.
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

ROSTER_SLOTS = [
    ("C", ["C"], 2, True),
    ("LW", ["LW"], 2, True),
    ("RW", ["RW"], 2, True),
    ("D", ["D"], 4, True),
    ("UTIL", ["C", "LW", "RW", "D"], 1, True),
    ("G", ["G"], 1, True),
    ("BENCH", ["C", "LW", "RW", "D", "G"], 8, False),
]


def main():
    Base.metadata.create_all(bind=engine)  # no-op for tables that already exist
    db = SessionLocal()

    existing = db.query(Universe).filter(Universe.code == "nhl").one_or_none()
    if existing is not None:
        print(f"'nhl' universe already exists (id={existing.id}) on {DATABASE_URL} - nothing to do.")
        return

    nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL")
    db.add(nhl)
    db.flush()

    for slot_code, eligible, count, is_starter in ROSTER_SLOTS:
        db.add(RosterSlotDefinition(
            universe_id=nhl.id, slot_code=slot_code, eligible_positions=eligible,
            slot_count=count, is_starter=is_starter,
        ))
    db.commit()

    print(f"Seeded 'nhl' universe (id={nhl.id}) and {len(ROSTER_SLOTS)} roster slot "
          f"definitions on {DATABASE_URL}.")


if __name__ == "__main__":
    main()
