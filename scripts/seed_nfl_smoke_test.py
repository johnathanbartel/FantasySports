"""Offline, in-memory NFL universe, league-creation, and draft smoke test.

The test exercises the real FastAPI routes without creating or modifying a
database file. It intentionally does not test NFL scoring, which still needs a
real-data scored-week validation.

Run from the repository root:

    .venv-api/Scripts/python.exe scripts/seed_nfl_smoke_test.py
"""

import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    DefenseGroup,
    DraftableAsset,
    LeagueUniverse,
    Player,
    RealTeam,
    RosterEntry,
    User,
)
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.sports.football.models import Play, PlayCredit, WeeklyPlayerScore  # noqa: F401
from seed_nfl_universe import seed_nfl_reference_data


engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    with TestingSession() as db:
        yield db


def seed_reference_data() -> User:
    with TestingSession() as db:
        nfl, inserted = seed_nfl_reference_data(db)
        assert inserted == 10
        _, inserted = seed_nfl_reference_data(db)
        assert inserted == 0

        real_teams = [
            RealTeam(universe_id=nfl.id, name="Seattle", external_ref="SEA"),
            RealTeam(universe_id=nfl.id, name="San Francisco", external_ref="SF"),
        ]
        db.add_all(real_teams)
        db.flush()

        assets = []
        for team in real_teams:
            for group in ("DL", "LB", "DB"):
                defense = DefenseGroup(real_team_id=team.id, position_group=group)
                db.add(defense)
                db.flush()
                assets.append(DraftableAsset(
                    universe_id=nfl.id,
                    asset_type="defense_group",
                    defense_group_id=defense.id,
                ))

        positions = ["QB", "RB", "RB", "WR", "WR", "TE", "K"]
        for index in range(34):
            team = real_teams[index % len(real_teams)]
            player = Player(
                universe_id=nfl.id,
                real_team_id=team.id,
                full_name=f"NFL Test Player {index + 1}",
                position=positions[index % len(positions)],
            )
            db.add(player)
            db.flush()
            assets.append(DraftableAsset(
                universe_id=nfl.id,
                asset_type="player",
                player_id=player.id,
            ))

        db.add_all(assets)
        commissioner = User(email="nfl-commissioner@example.com", display_name="NFL Commissioner")
        db.add(commissioner)
        db.commit()
        db.refresh(commissioner)
        return commissioner


def main() -> None:
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_db] = override_get_db
    commissioner = seed_reference_data()

    try:
        with TestClient(app) as client:
            response = client.post("/sessions", json={"user_id": commissioner.id})
            assert response.status_code == 200, response.text
            headers = {"Authorization": f"Bearer {response.json()['token']}"}

            response = client.post("/leagues", json={
                "name": "NFL v1 Smoke League",
                "commissioner_user_id": commissioner.id,
                "universe_codes": ["nfl"],
                "size": 2,
                "draft_rounds": 20,
                "draft_type": "snake",
            }, headers=headers)
            assert response.status_code == 200, response.text
            league = response.json()

            response = client.post("/leagues", json={
                "name": "Unsupported Individual NFL League",
                "commissioner_user_id": commissioner.id,
                "universe_codes": ["nfl"],
                "size": 2,
                "draft_rounds": 20,
                "defense_modes": {"nfl": "individual"},
            }, headers=headers)
            assert response.status_code == 400, response.text
            assert "don't support individual defense" in response.json()["detail"]

            slots = client.get(f"/leagues/{league['id']}/roster-slots").json()
            assert sum(slot["slot_count"] for slot in slots) == 20, slots
            assert sum(slot["slot_count"] for slot in slots if slot["is_starter"]) == 11, slots
            assert next(slot for slot in slots if slot["slot_code"] == "BENCH")["slot_count"] == 9

            for name in ("Puget Sound", "Bay Area"):
                response = client.post("/teams", json={
                    "league_id": league["id"],
                    "name": name,
                    "owner_user_id": commissioner.id,
                    "program_size": "small",
                }, headers=headers)
                assert response.status_code == 200, response.text

            response = client.post("/drafts/start", json={"league_id": league["id"]})
            assert response.status_code == 200, response.text
            draft = response.json()

            for _ in range(40):
                response = client.post(f"/drafts/{draft['id']}/autopick")
                assert response.status_code == 200, response.text

            picks = client.get(f"/drafts/{draft['id']}/picks").json()
            board = client.get(f"/drafts/{draft['id']}/board").json()
            assert len(picks) == 40 and all(pick["draftable_asset_id"] for pick in picks), picks
            assert board == [], board
            assert client.post(f"/drafts/{draft['id']}/autopick").status_code == 400

        with TestingSession() as db:
            link = db.query(LeagueUniverse).filter_by(league_id=league["id"]).one()
            assert link.defense_mode == "group"
            assert db.query(RosterEntry).count() == 40

        print("NFL smoke test passed:")
        print("- shared-platform NFL league created with group defense")
        print("- roster exposes 11 starters + 9 bench = 20 assets")
        print("- two-team, 20-round snake draft filled all 40 picks")
        print("- individual defense rejected until attribution is implemented")
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


if __name__ == "__main__":
    main()
