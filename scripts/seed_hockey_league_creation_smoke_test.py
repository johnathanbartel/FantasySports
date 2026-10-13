"""Offline smoke test for first-run NHL league creation.

Uses a temporary SQLite database.  It verifies that a deployment with no
seeded users or universes can create a guest commissioner, establish the NHL
reference rows on demand, create a league, and add the commissioner's team.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ["DATABASE_URL"] = "sqlite:///file:nhl-create-smoke?mode=memory&cache=shared&uri=true"

from fantasy_sports.api.routers.leagues import create_league
from fantasy_sports.api.routers.sessions import GuestSessionRequest, guest_login
from fantasy_sports.api.routers.teams import create_team
from fantasy_sports.api.schemas import LeagueCreateRequest, TeamCreateRequest
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import LeagueUniverse, RosterSlotDefinition, Universe, User
from fantasy_sports.platform.db.session import SessionLocal, engine


def main():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    session = guest_login(GuestSessionRequest(display_name="NHL Commissioner"), db)
    user = db.get(User, session.user_id)
    assert user is not None

    league = create_league(LeagueCreateRequest(
        name="First NHL League",
        commissioner_user_id=session.user_id,
        universe_codes=["nhl"],
        size=12,
        draft_rounds=None,
        draft_type="snake",
    ), db, user)
    assert league.draft_rounds == 24, league

    team = create_team(TeamCreateRequest(
        league_id=league.id,
        name="Commissioner's Team",
        program_size="large",
        owner_user_id=session.user_id,
        is_ai_owned=False,
    ), db, user)
    assert team.owner_user_id == session.user_id

    nhl = db.query(Universe).filter_by(code="nhl").one()
    link = db.query(LeagueUniverse).filter_by(league_id=league.id).one()
    assert link.universe_id == nhl.id
    slots = {
        row.slot_code: (row.slot_count, row.is_starter, set(row.eligible_positions))
        for row in db.query(RosterSlotDefinition).filter_by(universe_id=nhl.id)
    }
    assert slots == {
        "C": (1, True, {"C"}),
        "W": (2, True, {"W", "LW", "RW"}),
        "D": (2, True, {"D"}),
        "G": (1, True, {"G"}),
        "BENCH": (18, False, {"C", "W", "LW", "RW", "D", "G"}),
    }
    same_nhl, inserted = seed_hockey_reference_data(db)
    assert same_nhl.id == nhl.id
    assert inserted == 0

    print("NHL league creation smoke test passed: guest session, league, team, 6-position lineup, idempotency.")
    db.close()


if __name__ == "__main__":
    main()
    engine.dispose()
