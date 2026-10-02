"""Offline, in-memory NFL universe, league-creation, and draft smoke test for all three
defense modes (group, team, individual).

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
        assert inserted == 17
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
            for group in ("DL", "LB", "DB", "DEF"):
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

        # individual defenders: ordinary players with real roster positions (not normalized)
        for index, position in enumerate(["DE", "DT", "OLB", "MLB", "CB", "FS"] * 3):
            player = Player(
                universe_id=nfl.id,
                real_team_id=real_teams[index % len(real_teams)].id,
                full_name=f"NFL Test Defender {index + 1}",
                position=position,
            )
            db.add(player)
            db.flush()
            assets.append(DraftableAsset(universe_id=nfl.id, asset_type="player", player_id=player.id))

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
                "draft_type": "snake",
            }, headers=headers)
            assert response.status_code == 200, response.text
            league = response.json()

            response = client.post("/leagues", json={
                "name": "Bad Mode", "commissioner_user_id": commissioner.id, "universe_codes": ["nfl"],
                "size": 2, "draft_rounds": 20, "defense_modes": {"nfl": "bogus"},
            }, headers=headers)
            assert response.status_code == 400, response.text

            # draft_rounds defaults to the roster size: 11 starters + bench of 70% of starters (8)
            assert league["draft_rounds"] == 19, league
            slots = client.get(f"/leagues/{league['id']}/roster-slots").json()
            assert sum(slot["slot_count"] for slot in slots) == 19, slots
            assert sum(slot["slot_count"] for slot in slots if slot["is_starter"]) == 11, slots
            assert next(slot for slot in slots if slot["slot_code"] == "BENCH")["slot_count"] == 8

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

            for _ in range(38):
                response = client.post(f"/drafts/{draft['id']}/autopick")
                assert response.status_code == 200, response.text

            picks = client.get(f"/drafts/{draft['id']}/picks").json()
            board = client.get(f"/drafts/{draft['id']}/board").json()
            assert len(picks) == 38 and all(pick["draftable_asset_id"] for pick in picks), picks
            assert len(board) == 2, board  # 34 players + 6 group units = 40; DEF and defenders never enter
            assert client.post(f"/drafts/{draft['id']}/autopick").status_code == 400

            # team and individual leagues: pool holds only that mode's defense assets
            for mode, starters, bench, total in (
                ("team", 9, 6, 15), ("individual", 19, 13, 32),
            ):
                response = client.post("/leagues", json={
                    "name": f"NFL {mode} league", "commissioner_user_id": commissioner.id,
                    "universe_codes": ["nfl"], "size": 2, "draft_type": "snake",
                    "defense_modes": {"nfl": mode},
                }, headers=headers)
                assert response.status_code == 200, response.text
                lid = response.json()["id"]
                assert response.json()["draft_rounds"] == total, (mode, response.json())
                slots = client.get(f"/leagues/{lid}/roster-slots").json()
                assert sum(s["slot_count"] for s in slots) == total, (mode, slots)
                assert sum(s["slot_count"] for s in slots if s["is_starter"]) == starters, (mode, slots)
                assert next(s for s in slots if s["slot_code"] == "BENCH")["slot_count"] == bench, (mode, slots)
                for name in ("A", "B"):
                    r = client.post("/teams", json={"league_id": lid, "name": f"{mode} {name}",
                                                    "owner_user_id": commissioner.id, "program_size": "small"},
                                    headers=headers)
                    assert r.status_code == 200, r.text
                d = client.post("/drafts/start", json={"league_id": lid}).json()
                board = client.get(f"/drafts/{d['id']}/board").json()
                names = [a["display_name"] for a in board]
                has_team = any(n.endswith(" DEF") for n in names)
                has_group = any(n.split()[-1] in ("DL", "LB", "DB") and "Defender" not in n for n in names)
                has_defender = any("Defender" in n for n in names)
                assert (has_team, has_group, has_defender) == (
                    mode == "team", False, mode == "individual"), (mode, has_team, has_group, has_defender)
                if mode == "individual":
                    positions = {a["position"] for a in board if "Defender" in a["display_name"]}
                    assert positions == {"DL", "LB", "DB"}, positions
                for _ in range(12):
                    assert client.post(f"/drafts/{d['id']}/autopick").status_code == 200

        with TestingSession() as db:
            link = db.query(LeagueUniverse).filter_by(league_id=league["id"]).one()
            assert link.defense_mode == "group"
            assert db.query(RosterEntry).count() == 38 + 2 * 12  # group league + team and individual leagues

        print("NFL smoke test passed:")
        print("- shared-platform NFL league created with group defense; group pool excludes DEF and individual defenders")
        print("- roster exposes 11 starters + 8 bench = 19 assets (draft_rounds defaults to roster size)")
        print("- two-team, 19-round snake draft filled all 38 picks")
        print("- team (15 assets) and individual (32 assets) leagues draft only their own mode's defense")
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


if __name__ == "__main__":
    main()
