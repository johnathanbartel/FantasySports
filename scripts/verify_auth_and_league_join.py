"""End-to-end verification of user authentication, registration, session management,
and multi-user hockey league join flow.

Verifies:
1. Registration with email and hashed password.
2. Duplicate email rejection (409).
3. Email/password validation errors (400).
4. Login with valid credentials issuing a bearer session token.
5. Rejection of invalid passwords (401).
6. Current user profile lookup (/sessions/me).
7. Session logout and revocation.
8. Commissioner creates an NHL league.
9. A second registered user joins the league with their own team.
10. The draft starts and the joined user makes a draft pick for their team.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Mock external network calls to keep test offline and hermetic
_fetch_patcher = patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None)
_fetch_patcher.start()

os.environ["DATABASE_URL"] = "sqlite:///file:auth-join-smoke?mode=memory&cache=shared&uri=true"

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import DraftableAsset, Player, RealTeam, Universe
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data


def check(cond, msg="check failed"):
    if not cond:
        raise AssertionError(msg)


def main():
    Base.metadata.create_all(bind=engine)
    client = TestClient(app)

    # 1. Registration
    r = client.post("/sessions/register", json={
        "email": "COMMISSIONER@test.com",
        "display_name": "Commissioner Bob",
        "password": "secretpassword123",
    })
    check(r.status_code == 200, f"Register failed: {r.text}")
    commish_data = r.json()
    check(commish_data["email"] == "commissioner@test.com", "Email was not normalized to lowercase")
    check(commish_data["display_name"] == "Commissioner Bob")
    commish_token = commish_data["token"]
    commish_id = commish_data["user_id"]

    # 2. Duplicate registration rejection
    r = client.post("/sessions/register", json={
        "email": "commissioner@test.com",
        "display_name": "Another Name",
        "password": "password456",
    })
    check(r.status_code == 409, f"Expected 409 duplicate conflict, got: {r.status_code}")

    # 3. Validation
    r = client.post("/sessions/register", json={
        "email": "notanemail",
        "display_name": "Name",
        "password": "short",
    })
    check(r.status_code in (400, 422), f"Expected 400/422 validation error, got: {r.status_code}")

    # 4. Login with correct credentials
    r = client.post("/sessions/login", json={
        "email": "commissioner@test.com",
        "password": "secretpassword123",
    })
    check(r.status_code == 200, f"Login failed: {r.text}")
    login_data = r.json()
    check(login_data["user_id"] == commish_id)

    # 5. Login with invalid password
    r = client.post("/sessions/login", json={
        "email": "commissioner@test.com",
        "password": "wrongpassword",
    })
    check(r.status_code == 401, f"Expected 401 unauthorized, got: {r.status_code}")

    # 6. Current user (/sessions/me)
    r = client.get("/sessions/me", headers={"Authorization": f"Bearer {commish_token}"})
    check(r.status_code == 200, f"/sessions/me failed: {r.text}")
    me = r.json()
    check(me["id"] == commish_id)
    check(me["display_name"] == "Commissioner Bob")
    check(me["email"] == "commissioner@test.com")

    # 7. Register a second user (Player 2)
    r = client.post("/sessions/register", json={
        "email": "player2@test.com",
        "display_name": "Alice Player",
        "password": "mypassword99",
    })
    check(r.status_code == 200)
    p2_data = r.json()
    p2_token = p2_data["token"]
    p2_id = p2_data["user_id"]

    # 8. Seed reference hockey universe and a small player pool
    with SessionLocal() as db:
        nhl, _ = seed_hockey_reference_data(db)
        team = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
        db.add(team)
        db.flush()
        # Seed 60 assets so a draft can run
        for i in range(60):
            pos = ["C", "W", "D", "G"][i % 4]
            p = Player(universe_id=nhl.id, full_name=f"Skater {i}", position=pos, real_team_id=team.id, active=True)
            db.add(p)
            db.flush()
            db.add(DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id))
        db.commit()

    # 9. Commissioner creates an NHL League (2 teams, 18 rounds for testing)
    r = client.post("/leagues", headers={"Authorization": f"Bearer {commish_token}"}, json={
        "name": "Tonight NHL Draft League",
        "commissioner_user_id": commish_id,
        "universe_codes": ["nhl"],
        "size": 2,
        "draft_rounds": 18,
        "draft_type": "snake",
    })
    check(r.status_code == 200, f"League creation failed: {r.text}")
    league = r.json()
    league_id = league["id"]
    check(league["draft_rounds"] == 18)

    # Commissioner adds their own team
    r = client.post("/teams", headers={"Authorization": f"Bearer {commish_token}"}, json={
        "league_id": league_id,
        "name": "Commish Ice Kings",
        "program_size": "large",
        "owner_user_id": commish_id,
        "is_ai_owned": False,
    })
    check(r.status_code == 200, f"Commissioner team creation failed: {r.text}")
    commish_team = r.json()

    # 10. Player 2 joins the league with their own team
    r = client.post("/teams", headers={"Authorization": f"Bearer {p2_token}"}, json={
        "league_id": league_id,
        "name": "Alice All-Stars",
        "program_size": "large",
        "owner_user_id": p2_id,
        "is_ai_owned": False,
    })
    check(r.status_code == 200, f"Player 2 join failed: {r.text}")
    p2_team = r.json()
    check(p2_team["owner_user_id"] == p2_id)

    # 11. Commissioner starts the draft
    r = client.post("/drafts/start", headers={"Authorization": f"Bearer {commish_token}"}, json={
        "league_id": league_id,
        "timed": False,
    })
    check(r.status_code == 200, f"Draft start failed: {r.text}")
    draft_info = r.json()
    draft_id = draft_info["id"]

    # 12. Check draft board
    r = client.get(f"/drafts/{draft_id}/board")
    check(r.status_code == 200)
    board = r.json()
    check(len(board) >= 50)
    first_asset = board[0]["draftable_asset_id"]

    # 13. Pick 1 is Team 1 (Commissioner)
    r = client.post(f"/drafts/{draft_id}/pick", headers={"Authorization": f"Bearer {commish_token}"}, json={
        "draftable_asset_id": first_asset,
    })
    check(r.status_code == 200, f"Commissioner pick failed: {r.text}")

    # Pick 2 is Team 2 (Player 2 / Alice)
    second_asset = board[1]["draftable_asset_id"]
    # Commissioner cannot make Alice's pick (403)
    r = client.post(f"/drafts/{draft_id}/pick", headers={"Authorization": f"Bearer {commish_token}"}, json={
        "draftable_asset_id": second_asset,
    })
    check(r.status_code == 403, f"Expected 403 when commissioner drafts for Alice, got {r.status_code}")

    # Alice makes her pick with her own session token
    r = client.post(f"/drafts/{draft_id}/pick", headers={"Authorization": f"Bearer {p2_token}"}, json={
        "draftable_asset_id": second_asset,
    })
    check(r.status_code == 200, f"Player 2 pick failed: {r.text}")
    pick_result = r.json()
    check(pick_result["team_id"] == p2_team["id"])

    # 14. Logout test
    r = client.post("/sessions/logout", headers={"Authorization": f"Bearer {commish_token}"})
    check(r.status_code == 200)
    # Token should now be invalid
    r = client.get("/sessions/me", headers={"Authorization": f"Bearer {commish_token}"})
    check(r.status_code == 401, "Revoked token should fail with 401")

    print("All auth, registration, session management, and multi-user hockey draft tests passed!")


if __name__ == "__main__":
    main()
    engine.dispose()
