"""Exercise the snake-draft duplicate-roster guard through the HTTP API.

Uses its own throwaway SQLite file (duplicate_pick_smoke_test.db) and deletes
any previous copy before seeding. Run from the repo root with the API venv:

    .venv-api/Scripts/python.exe scripts/seed_duplicate_pick_smoke_test.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["DATABASE_URL"] = "sqlite:///./duplicate_pick_smoke_test.db"

db_path = Path("duplicate_pick_smoke_test.db")
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    DraftPick,
    DraftableAsset,
    League,
    LeagueUniverse,
    Player,
    RealTeam,
    RosterEntry,
    Team,
    Universe,
    User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine

Base.metadata.create_all(bind=engine)
client = TestClient(app)


def seed_data(db):
    universe = Universe(
        sport="football",
        level="college",
        code="duplicate-pick-smoke",
        display_name="Duplicate Pick Smoke",
    )
    first_owner = User(email="first@example.com", display_name="First Owner")
    second_owner = User(email="second@example.com", display_name="Second Owner")
    db.add_all([universe, first_owner, second_owner])
    db.flush()

    league = League(
        name="Duplicate Pick Smoke League",
        commissioner_user_id=first_owner.id,
        size=2,
        draft_rounds=1,
        draft_type="snake",
    )
    real_team = RealTeam(universe_id=universe.id, name="Smoke State")
    db.add_all([league, real_team])
    db.flush()

    db.add(LeagueUniverse(league_id=league.id, universe_id=universe.id))
    first_team = Team(league_id=league.id, name="First Team", owner_user_id=first_owner.id)
    second_team = Team(league_id=league.id, name="Second Team", owner_user_id=second_owner.id)
    db.add_all([first_team, second_team])
    db.flush()

    first_player = Player(
        universe_id=universe.id,
        real_team_id=real_team.id,
        full_name="First Player",
        position="QB",
    )
    second_player = Player(
        universe_id=universe.id,
        real_team_id=real_team.id,
        full_name="Second Player",
        position="RB",
    )
    db.add_all([first_player, second_player])
    db.flush()

    first_asset = DraftableAsset(
        universe_id=universe.id,
        asset_type="player",
        player_id=first_player.id,
    )
    second_asset = DraftableAsset(
        universe_id=universe.id,
        asset_type="player",
        player_id=second_player.id,
    )
    db.add_all([first_asset, second_asset])
    db.commit()

    return first_owner, second_owner, first_team, second_team, league, first_asset, second_asset


def login(user_id):
    response = client.post("/sessions", json={"user_id": user_id})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def main():
    db = SessionLocal()
    try:
        first_owner, second_owner, first_team, second_team, league, first_asset, second_asset = (
            seed_data(db)
        )
        first_headers = login(first_owner.id)
        second_headers = login(second_owner.id)

        response = client.post("/drafts/start", json={"league_id": league.id})
        assert response.status_code == 200, response.text
        draft_id = response.json()["id"]

        response = client.post(
            f"/drafts/{draft_id}/pick",
            json={"draftable_asset_id": first_asset.id},
            headers=first_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["team_id"] == first_team.id
        print(f"Valid pick recorded: asset {first_asset.id} -> team {first_team.id}")

        response = client.post(
            f"/drafts/{draft_id}/pick",
            json={"draftable_asset_id": first_asset.id},
            headers=second_headers,
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "Draftable asset is already rostered in this league"

        db.expire_all()
        picks = db.query(DraftPick).filter(DraftPick.draft_id == draft_id).order_by(DraftPick.pick_number).all()
        assert picks[0].draftable_asset_id == first_asset.id
        assert picks[1].draftable_asset_id is None
        assert db.query(RosterEntry).filter(RosterEntry.draftable_asset_id == first_asset.id).count() == 1
        assert db.query(RosterEntry).count() == 1
        print("Duplicate pick rejected with 409 and no pick or roster row recorded")

        response = client.post(
            f"/drafts/{draft_id}/pick",
            json={"draftable_asset_id": second_asset.id},
            headers=second_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["team_id"] == second_team.id

        db.expire_all()
        assert db.query(DraftPick).filter(DraftPick.draftable_asset_id.isnot(None)).count() == 2
        assert db.query(RosterEntry).count() == 2
        print(f"Different asset recorded: asset {second_asset.id} -> team {second_team.id}")
        print("Duplicate-pick smoke test passed.")
    finally:
        db.close()
        client.close()


if __name__ == "__main__":
    main()
