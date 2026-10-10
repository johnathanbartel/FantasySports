"""
Stands up a blank NHL project inside the existing multi-sport platform and
round-trips it through the real HTTP API (via FastAPI's TestClient): create
an "nhl" universe + roster slot definitions (see
src/fantasy_sports/sports/hockey/README.md) -> seed a couple of real NHL
teams and players across every hockey position -> create a league on that
universe -> run a snake draft covering a skater and a goalie -> read the
pick history back out through the API.

This does not exercise hockey scoring - the xG credit-attribution pipeline
described in the README is a data-model-only stand-up at this stage, not
implemented yet (see README.md's Data pipeline section).

Uses its own throwaway SQLite file (hockey_smoke_test.db) so it never
touches fantasy.db or any other real dev database. Run from the repo root
with the API venv:

    .venv-api/Scripts/python.exe scripts/seed_hockey_smoke_test.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["DATABASE_URL"] = "sqlite:///./hockey_smoke_test.db"

# Fresh DB file every run.
db_path = Path("hockey_smoke_test.db")
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.platform.db.models.core import (
    DraftableAsset,
    Player,
    RealTeam,
    Team,
    User,
)

# Registers the hockey-specific tables (shots/shot_credits) on Base.metadata
# even though this smoke test doesn't use them yet - proves the data model
# import-wires cleanly into the shared schema.
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app)

def seed_reference_data(db):
    """Reference/admin data - not something a user action creates."""
    nhl, _ = seed_hockey_reference_data(db)

    bruins = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
    leafs = RealTeam(universe_id=nhl.id, name="Toronto Maple Leafs", external_ref="TOR")
    db.add_all([bruins, leafs])
    db.flush()

    players = [
        Player(universe_id=nhl.id, real_team_id=bruins.id, full_name="David Pastrnak", position="RW"),
        Player(universe_id=nhl.id, real_team_id=bruins.id, full_name="Charlie McAvoy", position="D"),
        Player(universe_id=nhl.id, real_team_id=bruins.id, full_name="Jeremy Swayman", position="G"),
        Player(universe_id=nhl.id, real_team_id=leafs.id, full_name="Auston Matthews", position="C"),
        Player(universe_id=nhl.id, real_team_id=leafs.id, full_name="William Nylander", position="RW"),
    ]
    db.add_all(players)
    db.flush()

    assets = [
        DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id) for p in players
    ]
    db.add_all(assets)
    db.flush()

    commissioner = User(email="johnathan@example.com", display_name="Johnathan")
    db.add(commissioner)
    db.commit()

    by_name = {p.full_name: a for p, a in zip(players, assets)}
    return {"nhl": nhl, "assets_by_name": by_name, "commissioner": commissioner}


def main():
    db = SessionLocal()
    ref = seed_reference_data(db)

    resp = client.post("/sessions", json={"user_id": ref["commissioner"].id})
    assert resp.status_code == 200, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['token']}"}

    # --- League + teams -----------------------------------------------
    resp = client.post("/leagues", json={
        "name": "NHL Stand-Up League",
        "commissioner_user_id": ref["commissioner"].id,
        "universe_codes": ["nhl"],
        "size": 2,
        "draft_rounds": 2,
    }, headers=headers)
    assert resp.status_code == 200, resp.text
    league = resp.json()
    print(f"Created league {league['id']}: {league['name']} (universe: nhl)")

    team_a = Team(league_id=league["id"], name="Team A", owner_user_id=ref["commissioner"].id)
    team_b = Team(league_id=league["id"], name="Team B", owner_user_id=ref["commissioner"].id)
    db.add_all([team_a, team_b])
    db.commit()
    print(f"Teams: {team_a.id} ({team_a.name}), {team_b.id} ({team_b.name})")

    # --- Draft: a skater and a goalie from the same universe pool -------
    resp = client.post("/drafts/start", json={"league_id": league["id"]})
    assert resp.status_code == 200, resp.text
    draft = resp.json()
    print(f"Started draft {draft['id']}, status={draft['status']}")

    board = client.get(f"/drafts/{draft['id']}/board").json()
    print(f"Draft board: {[b['display_name'] for b in board]}")

    matthews = ref["assets_by_name"]["Auston Matthews"]
    swayman = ref["assets_by_name"]["Jeremy Swayman"]

    resp = client.post(
        f"/drafts/{draft['id']}/pick", json={"draftable_asset_id": matthews.id}, headers=headers,
    )
    assert resp.status_code == 200, resp.text
    print(f"Pick 1: {resp.json()['display_name']} -> team {resp.json()['team_id']}")

    resp = client.post(
        f"/drafts/{draft['id']}/pick", json={"draftable_asset_id": swayman.id}, headers=headers,
    )
    assert resp.status_code == 200, resp.text
    print(f"Pick 2: {resp.json()['display_name']} -> team {resp.json()['team_id']} "
          f"(a goalie, drafted from the same pool as the center above)")

    # Fill remaining picks via autopick.
    for _ in range(2):
        resp = client.post(f"/drafts/{draft['id']}/autopick")
        if resp.status_code == 200:
            print(f"Autopick: {resp.json()['display_name']} -> team {resp.json()['team_id']}")

    picks = client.get(f"/drafts/{draft['id']}/picks").json()
    print(f"Full pick history ({len(picks)} picks): "
          f"{[(p['round'], p['pick_number'], p['display_name']) for p in picks]}")

    resp = client.get("/health")
    assert resp.status_code == 200, resp.text
    print(f"\nAPI health check: {resp.json()}")

    print("\nNHL stand-up smoke test passed: universe + roster slots + draft "
          "round-tripped end to end on the shared platform.")


if __name__ == "__main__":
    main()
