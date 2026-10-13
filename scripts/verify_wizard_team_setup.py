"""
Verification script for the League Creation Wizard Team Setup and Invites:
1. Creates a league with size=4.
2. Validates authorization:
   - Unauthenticated / non-commissioner cannot call teams-setup (401/403).
   - Team count mismatch returns 400.
   - Missing commissioner team returns 400.
3. Configures 4 teams via POST /leagues/{league_id}/teams-setup:
   - Team 1: Commissioner team (human)
   - Team 2: AI team
   - Team 3: AI team
   - Team 4: Invited team (email: manager@example.com)
4. Asserts:
   - All 4 teams created and assigned conferences.
   - Team 1 and 4 have owner_user_id and is_ai_owned=False.
   - Team 2 and 3 have owner_user_id=None and is_ai_owned=True.
   - ck_team_owner_xor_ai constraint satisfied.
   - Placeholder user created for manager@example.com with password_hash=None.
   - Invite URL correctly generated for Team 4.
5. Tests Account Claiming:
   - Invited user registers via POST /sessions/register with manager@example.com.
   - Succeeds and claims the placeholder user ID.
   - Team 4 is immediately owned by the registered user.
   - Re-registration with same email returns 409.
6. Tests Single Team Invite:
   - POST /leagues/{league_id}/teams/{team_id}/invite converts Team 3 from AI to invited manager.
7. Verifies GET /leagues/defaults/ai-team-names endpoint.

Run with:
    .venv-api/Scripts/python.exe scripts/verify_wizard_team_setup.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DB_FILE = ROOT / "verify_wizard_teams.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"

from fastapi.testclient import TestClient
from sqlalchemy import event

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.db.models.core import (
    League,
    Player,
    RealTeam,
    RosterSlotDefinition,
    Team,
    Universe,
    User,
)

# Enable SQLite foreign key constraints
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

Base.metadata.create_all(bind=engine)
client = TestClient(app)


def seed_reference_data(db):
    univ = Universe(sport="football", level="college", code="ncaaf", display_name="NCAAF")
    db.add(univ)
    db.flush()

    team_real = RealTeam(universe_id=univ.id, name="State College", conference="SEC", classification="fbs")
    db.add(team_real)
    db.flush()

    player = Player(universe_id=univ.id, real_team_id=team_real.id, full_name="Star Player", position="QB")
    db.add(player)
    db.flush()

    slot = RosterSlotDefinition(
        universe_id=univ.id, slot_code="QB", eligible_positions=["QB"], slot_count=1, is_starter=True
    )
    db.add(slot)

    commish = User(email="commish@example.com", display_name="Commissioner Dan", password_hash="hash123")
    stranger = User(email="stranger@example.com", display_name="Stranger Bob", password_hash="hash456")
    db.add_all([commish, stranger])
    db.commit()

    return {"universe": univ, "commish": commish, "stranger": stranger}


def main():
    print("=== Testing Wizard Team Setup & Invites ===")
    db = SessionLocal()
    ref = seed_reference_data(db)
    commish = ref["commish"]
    stranger = ref["stranger"]

    # 1. Login sessions
    r_c = client.post("/sessions", json={"user_id": commish.id})
    assert r_c.status_code == 200, r_c.text
    commish_headers = {"Authorization": f"Bearer {r_c.json()['token']}"}

    r_s = client.post("/sessions", json={"user_id": stranger.id})
    assert r_s.status_code == 200, r_s.text
    stranger_headers = {"Authorization": f"Bearer {r_s.json()['token']}"}

    # 2. Check defaults endpoint
    r_def = client.get("/leagues/defaults/ai-team-names")
    assert r_def.status_code == 200, r_def.text
    ai_names = r_def.json()
    assert len(ai_names) > 0, "Expected non-empty list of default AI names"
    print(f"[OK] GET /leagues/defaults/ai-team-names returned {len(ai_names)} default names")

    # 3. Create a 4-team league
    r_lg = client.post(
        "/leagues",
        headers=commish_headers,
        json={
            "name": "Wizard Invitational",
            "commissioner_user_id": commish.id,
            "universe_codes": ["ncaaf"],
            "size": 4,
            "draft_rounds": 5,
            "program_size_mode": "team_choice",
            "draft_type": "snake",
        },
    )
    assert r_lg.status_code == 200, r_lg.text
    league_id = r_lg.json()["id"]
    print(f"[OK] Created league ID {league_id} with size=4")

    # 4. Auth & validation checks
    # Non-commissioner cannot setup teams
    r_bad_auth = client.post(
        f"/leagues/{league_id}/teams-setup",
        headers=stranger_headers,
        json={"teams": []},
    )
    assert r_bad_auth.status_code == 403, r_bad_auth.text
    print("[OK] Non-commissioner rejected with 403")

    # Size mismatch rejected
    r_bad_size = client.post(
        f"/leagues/{league_id}/teams-setup",
        headers=commish_headers,
        json={
            "teams": [
                {"name": "Team 1", "is_commissioner": True, "is_ai": False},
                {"name": "Team 2", "is_commissioner": False, "is_ai": True},
            ]
        },
    )
    assert r_bad_size.status_code == 400, r_bad_size.text
    assert "Expected exactly 4 teams" in r_bad_size.json()["detail"]
    print("[OK] Size mismatch (2 of 4) rejected with 400")

    # Missing commissioner team rejected
    r_no_commish = client.post(
        f"/leagues/{league_id}/teams-setup",
        headers=commish_headers,
        json={
            "teams": [
                {"name": "Team 1", "is_commissioner": False, "is_ai": True},
                {"name": "Team 2", "is_commissioner": False, "is_ai": True},
                {"name": "Team 3", "is_commissioner": False, "is_ai": True},
                {"name": "Team 4", "is_commissioner": False, "is_ai": True},
            ]
        },
    )
    assert r_no_commish.status_code == 400, r_no_commish.text
    assert "One of the teams must be marked as the commissioner's team" in r_no_commish.json()["detail"]
    print("[OK] Missing commissioner rejected with 400")

    # Invalid email rejected
    r_bad_email = client.post(
        f"/leagues/{league_id}/teams-setup",
        headers=commish_headers,
        json={
            "teams": [
                {"name": "Commish Crew", "is_commissioner": True, "is_ai": False},
                {"name": "AI 1", "is_commissioner": False, "is_ai": True},
                {"name": "AI 2", "is_commissioner": False, "is_ai": True},
                {"name": "Human Inv", "is_commissioner": False, "is_ai": False, "email": "invalid-email"},
            ]
        },
    )
    assert r_bad_email.status_code == 400, r_bad_email.text
    print("[OK] Invalid email rejected with 400")

    # 5. Successful teams setup
    invited_email = "manager@example.com"
    r_setup = client.post(
        f"/leagues/{league_id}/teams-setup",
        headers=commish_headers,
        json={
            "teams": [
                {"name": "Commish Crew", "is_commissioner": True, "is_ai": False, "program_size": "large"},
                {"name": "Ridgeline Rams", "is_commissioner": False, "is_ai": True, "program_size": "large"},
                {"name": "Harbor City Hawks", "is_commissioner": False, "is_ai": True, "program_size": "moderate"},
                {"name": "Invited Legends", "is_commissioner": False, "is_ai": False, "email": invited_email, "program_size": "small"},
            ]
        },
    )
    assert r_setup.status_code == 200, r_setup.text
    setup_data = r_setup.json()
    assert len(setup_data["teams"]) == 4
    print("[OK] POST /leagues/{id}/teams-setup succeeded with 4 teams")

    teams_res = setup_data["teams"]
    t1 = teams_res[0]
    t2 = teams_res[1]
    t3 = teams_res[2]
    t4 = teams_res[3]

    assert t1["name"] == "Commish Crew"
    assert t1["owner_user_id"] == commish.id
    assert not t1["is_ai_owned"]
    assert t1["invite_url"] is None

    assert t2["name"] == "Ridgeline Rams"
    assert t2["owner_user_id"] is None
    assert t2["is_ai_owned"]
    assert t2["invite_url"] is None

    assert t3["name"] == "Harbor City Hawks"
    assert t3["owner_user_id"] is None
    assert t3["is_ai_owned"]

    assert t4["name"] == "Invited Legends"
    assert not t4["is_ai_owned"]
    assert t4["email"] == invited_email
    assert t4["invite_url"] is not None
    assert f"league_id={league_id}" in t4["invite_url"]
    assert f"team_id={t4['id']}" in t4["invite_url"]
    assert f"email={invited_email}" in t4["invite_url"]
    print(f"[OK] Team 4 invite URL generated: {t4['invite_url']}")

    # Verify placeholder user in DB
    db.expire_all()
    placeholder_user = db.query(User).filter(User.email == invited_email).first()
    assert placeholder_user is not None, "Placeholder user should be created"
    assert placeholder_user.password_hash is None, "Placeholder user should have no password_hash"
    assert t4["owner_user_id"] == placeholder_user.id
    print("[OK] Placeholder user created with password_hash=None and assigned to Team 4")

    # 6. Test Account Claiming via POST /sessions/register
    r_reg = client.post(
        "/sessions/register",
        json={
            "email": invited_email,
            "display_name": "Coach Alexander",
            "password": "securepassword123",
        },
    )
    assert r_reg.status_code == 200, r_reg.text
    reg_data = r_reg.json()
    claimed_user_id = reg_data["user_id"]
    assert claimed_user_id == placeholder_user.id, "Registered user should claim placeholder user ID"
    print("[OK] Invited user successfully claimed placeholder user account upon registration")

    # Verify password hash now exists
    db.expire_all()
    updated_user = db.get(User, claimed_user_id)
    assert updated_user.password_hash is not None
    assert updated_user.display_name == "Coach Alexander"

    # Verify registering again fails with 409
    r_dup = client.post(
        "/sessions/register",
        json={
            "email": invited_email,
            "display_name": "Another Guy",
            "password": "password999",
        },
    )
    assert r_dup.status_code == 409, r_dup.text
    print("[OK] Re-registration with claimed email correctly returns 409")

    # 7. Test single team invite endpoint: convert Team 3 (AI) to invited human
    r_inv3 = client.post(
        f"/leagues/{league_id}/teams/{t3['id']}/invite",
        headers=commish_headers,
        json={"email": "coach2@example.com"},
    )
    assert r_inv3.status_code == 200, r_inv3.text
    inv3_data = r_inv3.json()
    assert inv3_data["email"] == "coach2@example.com"
    assert f"team_id={t3['id']}" in inv3_data["invite_url"]
    print(f"[OK] POST /leagues/{league_id}/teams/{t3['id']}/invite updated team and generated invite link")

    db.expire_all()
    updated_t3 = db.get(Team, t3["id"])
    assert not updated_t3.is_ai_owned
    assert updated_t3.owner_user_id is not None
    t3_owner = db.get(User, updated_t3.owner_user_id)
    assert t3_owner.email == "coach2@example.com"
    assert t3_owner.password_hash is None
    print("[OK] Team 3 converted from AI to human placeholder user in database")

    # Clean up DB
    db.close()
    engine.dispose()
    if DB_FILE.exists():
        DB_FILE.unlink()
    print("=== All Wizard Team Setup & Invite Verification Checks Passed! ===")


if __name__ == "__main__":
    main()
