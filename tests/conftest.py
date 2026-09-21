"""
Shared test fixtures.

Every test runs against a throwaway in-memory SQLite database created from
the ORM metadata - the same trick scripts/seed_smoke_test.py uses with a
temp file, so tests can never touch a real dev database. DATABASE_URL is
pinned before any application import for the same reason.
"""

import os

os.environ["DATABASE_URL"] = "sqlite://"  # in-memory; set before app import

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import fantasy_sports.platform.db.models  # noqa: E402,F401  (registers every model)
from fantasy_sports.api.main import app  # noqa: E402
from fantasy_sports.platform.db.base import Base  # noqa: E402
from fantasy_sports.platform.db.models.core import (  # noqa: E402
    DefenseGroup,
    DraftableAsset,
    League,
    LeagueUniverse,
    Player,
    RealTeam,
    Team,
    Universe,
    User,
)
from fantasy_sports.platform.db.session import get_db  # noqa: E402


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # one shared connection, so in-memory data persists
    )
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    yield session
    session.close()


@pytest.fixture
def client(db):
    """TestClient wired to the same session the test itself holds."""
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class LeagueFixture:
    """Handles for a seeded league, so tests read as bidding, not as setup."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture
def seeded_league(db):
    """A 3-team NCAAF league with a small, fully-enumerated asset pool.

    The pool deliberately contains all three of Georgia's defense groups plus
    Notre Dame's DL, because the defensive-bidding rule under test is that
    each group is its own independent auction line.
    """
    ncaaf = Universe(sport="football", level="college", code="ncaaf", display_name="NCAAF")
    db.add(ncaaf)
    db.flush()

    georgia = RealTeam(universe_id=ncaaf.id, name="Georgia", conference="SEC", classification="fbs")
    notre_dame = RealTeam(universe_id=ncaaf.id, name="Notre Dame", classification="fbs")
    db.add_all([georgia, notre_dame])
    db.flush()

    players = [
        Player(universe_id=ncaaf.id, real_team_id=georgia.id, full_name="Carson Beck", position="QB"),
        Player(universe_id=ncaaf.id, real_team_id=georgia.id, full_name="Trevor Etienne", position="RB"),
        Player(universe_id=ncaaf.id, real_team_id=notre_dame.id, full_name="Riley Leonard", position="QB"),
        Player(universe_id=ncaaf.id, real_team_id=notre_dame.id, full_name="Jeremiyah Love", position="RB"),
    ]
    db.add_all(players)
    db.flush()

    defense_groups = [
        DefenseGroup(real_team_id=georgia.id, position_group="DL"),
        DefenseGroup(real_team_id=georgia.id, position_group="LB"),
        DefenseGroup(real_team_id=georgia.id, position_group="DB"),
        DefenseGroup(real_team_id=notre_dame.id, position_group="DL"),
    ]
    db.add_all(defense_groups)
    db.flush()

    player_assets = {}
    for player in players:
        asset = DraftableAsset(universe_id=ncaaf.id, asset_type="player", player_id=player.id)
        db.add(asset)
        db.flush()
        player_assets[player.full_name] = asset

    defense_assets = {}
    for group in defense_groups:
        asset = DraftableAsset(
            universe_id=ncaaf.id, asset_type="defense_group", defense_group_id=group.id
        )
        db.add(asset)
        db.flush()
        real_team_name = "Georgia" if group.real_team_id == georgia.id else "Notre Dame"
        defense_assets[f"{real_team_name} {group.position_group}"] = asset

    commissioner = User(email="commish@example.com", display_name="Commish")
    db.add(commissioner)
    db.flush()

    league = League(
        name="Auction Test League",
        commissioner_user_id=commissioner.id,
        size=3,
        draft_rounds=3,
    )
    db.add(league)
    db.flush()
    db.add(LeagueUniverse(league_id=league.id, universe_id=ncaaf.id))

    teams = [
        Team(league_id=league.id, name="Alpha", owner_user_id=commissioner.id),
        Team(league_id=league.id, name="Bravo", is_ai_owned=True),
        Team(league_id=league.id, name="Charlie", is_ai_owned=True),
    ]
    db.add_all(teams)
    db.commit()

    return LeagueFixture(
        universe=ncaaf,
        league=league,
        teams=teams,
        team_ids=[t.id for t in teams],
        commissioner=commissioner,
        player_assets=player_assets,
        defense_assets=defense_assets,
    )
