"""Regression check for an NHL draft board without football stat tables.

Production can host NHL leagues before any college-football season data has
been installed. The draft board must fall back to alphabetical ordering in
that schema instead of querying the absent player_season_points table.
"""

import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fantasy_sports.api.routers.draft import get_draft_board
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    Draft,
    DraftPick,
    DraftableAsset,
    League,
    LeagueUniverse,
    Player,
    RealTeam,
    Team,
    Universe,
    User,
)


def main() -> None:
    engine = create_engine("sqlite://")
    tables = [
        User.__table__,
        Universe.__table__,
        League.__table__,
        LeagueUniverse.__table__,
        RealTeam.__table__,
        Player.__table__,
        DraftableAsset.__table__,
        Team.__table__,
        Draft.__table__,
        DraftPick.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    assert not inspect(engine).has_table("player_season_points")

    db = sessionmaker(bind=engine)()
    user = User(email="nhl-schema@example.com", display_name="NHL Schema")
    universe = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL")
    db.add_all([user, universe])
    db.flush()

    league = League(
        name="NHL Schema League",
        commissioner_user_id=user.id,
        size=12,
        draft_rounds=1,
        draft_type="snake",
    )
    db.add(league)
    db.flush()
    db.add(LeagueUniverse(league_id=league.id, universe_id=universe.id, defense_mode="group"))

    player = Player(
        universe_id=universe.id,
        full_name="Current NHL Player",
        position="C",
        external_ref="nhl-current",
        active=True,
    )
    db.add(player)
    db.flush()
    asset = DraftableAsset(universe_id=universe.id, asset_type="player", player_id=player.id)
    db.add(asset)
    db.flush()

    draft = Draft(league_id=league.id, status="in_progress")
    db.add(draft)
    db.commit()

    board = get_draft_board(draft.id, db)
    assert len(board) == 1
    assert board[0].draftable_asset_id == asset.id
    assert board[0].display_name == "Current NHL Player"
    print("NHL draft board passed without football season-points tables")


if __name__ == "__main__":
    main()
