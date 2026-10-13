"""
Verification script for Commissioner AI Team Assignment:
Tests that a commissioner at any point can assign an AI team to a user (by email or user_id),
rename the team, convert back to AI, and that all ownership, roster, and check constraints hold.

Run with:
    .venv-api/Scripts/python.exe scripts/verify_commissioner_assign_ai_team.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DB_FILE = ROOT / "verify_assign_ai_team.db"
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
    RosterEntry,
    RosterSlotDefinition,
    Team,
    Universe,
    User,
    DraftableAsset,
)

# Enable foreign keys
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

    asset = DraftableAsset(universe_id=univ.id, asset_type="player", player_id=player.id)
    db.add(asset)
    db.flush()

    slot = RosterSlotDefinition(
        universe_id=univ.id, slot_code="QB", eligible_positions=["QB"], slot_count=1, is_starter=True
    )
    db.add(slot)

    commish = User(email="commish@example.com", display_name="Commissioner Dan", password_hash="hash123")
    stranger = User(email="stranger@example.com", display_name="Stranger Bob", password_hash="hash456")
    existing_user = User(email="member@example.com", display_name="Member Alice", password_hash="hash789")
    db.add_all([commish, stranger, existing_user])
    db.commit()

    return {"universe": univ, "commish": commish, "stranger": stranger, "member": existing_user, "asset": asset}


def auth_header(user_id: int):
    res = client.post("/sessions", json={"user_id": user_id})
    assert res.status_code == 200, res.text
    token = res.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def main():
    print("=== Testing Commissioner AI Team Assignment ===")

    db = SessionLocal()
    ref = seed_reference_data(db)
    commish = ref["commish"]
    stranger = ref["stranger"]
    member = ref["member"]
    asset = ref["asset"]

    commish_headers = auth_header(commish.id)
    stranger_headers = auth_header(stranger.id)

    # 1. Create a league with 4 teams (1 commissioner, 3 AI)
    create_res = client.post(
        "/leagues",
        headers=commish_headers,
        json={
            "name": "Assign Test League",
            "commissioner_user_id": commish.id,
            "universe_codes": ["ncaaf"],
            "size": 4,
            "draft_rounds": 5,
            "program_size_mode": "team_choice",
            "draft_type": "snake",
        },
    )
    assert create_res.status_code == 200, create_res.text
    league_id = create_res.json()["id"]

    teams_setup_res = client.post(
        f"/leagues/{league_id}/teams-setup",
        headers=commish_headers,
        json={
            "teams": [
                {"name": "Dan's Dynasty", "is_commissioner": True, "is_ai": False, "program_size": "large"},
                {"name": "Alpha AI", "is_commissioner": False, "is_ai": True, "program_size": "moderate"},
                {"name": "Beta AI", "is_commissioner": False, "is_ai": True, "program_size": "small"},
                {"name": "Gamma AI", "is_commissioner": False, "is_ai": True, "program_size": "large"},
            ]
        },
    )
    assert teams_setup_res.status_code == 200, teams_setup_res.text
    setup_teams = teams_setup_res.json()["teams"]
    t1_id = setup_teams[0]["id"]
    t2_id = setup_teams[1]["id"]
    t3_id = setup_teams[2]["id"]
    t4_id = setup_teams[3]["id"]
    print(f"[OK] League {league_id} created with 1 human team and 3 AI teams")

    # 2. Authorization checks
    anon_res = client.post(f"/leagues/{league_id}/teams/{t2_id}/assign", json={"email": "hacker@example.com"})
    assert anon_res.status_code == 401, f"Expected 401 for anonymous call, got {anon_res.status_code}"

    stranger_res = client.post(
        f"/leagues/{league_id}/teams/{t2_id}/assign",
        headers=stranger_headers,
        json={"email": "hacker@example.com"},
    )
    assert stranger_res.status_code == 403, f"Expected 403 for stranger, got {stranger_res.status_code}"
    print("[OK] Unauthorized and non-commissioner calls successfully rejected (401 / 403)")

    # 3. Validation checks
    invalid_email_res = client.post(
        f"/leagues/{league_id}/teams/{t2_id}/assign",
        headers=commish_headers,
        json={"email": "not-an-email"},
    )
    assert invalid_email_res.status_code == 400, "Expected 400 for invalid email"

    no_target_res = client.post(
        f"/leagues/{league_id}/teams/{t2_id}/assign",
        headers=commish_headers,
        json={"is_ai": False},
    )
    assert no_target_res.status_code == 400, "Expected 400 when neither email nor user_id is provided"
    print("[OK] Invalid payloads rejected with 400")

    # 4. Assign Team 2 (Alpha AI) by email to a new user
    assign_email_res = client.post(
        f"/leagues/{league_id}/teams/{t2_id}/assign",
        headers=commish_headers,
        json={"email": "freshmanager@example.com", "team_name": "Fresh Force"},
    )
    assert assign_email_res.status_code == 200, assign_email_res.text
    data = assign_email_res.json()
    assert data["team_id"] == t2_id
    assert data["team_name"] == "Fresh Force"
    assert data["is_ai_owned"] is False
    assert data["owner_email"] == "freshmanager@example.com"
    assert "freshmanager@example.com" in data["invite_url"]
    assert data["status"] == "assigned"

    # Verify database state for Team 2
    t2_db = db.get(Team, t2_id)
    assert t2_db.is_ai_owned is False
    assert t2_db.owner_user_id is not None
    assert t2_db.name == "Fresh Force"
    target_u = db.get(User, t2_db.owner_user_id)
    assert target_u.email == "freshmanager@example.com"
    print(f"[OK] AI Team 2 assigned by email to placeholder user {target_u.id} with invite link")

    # 5. Assign Team 3 (Beta AI) by existing user ID
    assign_uid_res = client.post(
        f"/leagues/{league_id}/teams/{t3_id}/assign",
        headers=commish_headers,
        json={"user_id": member.id, "team_name": "Alice's Aces"},
    )
    assert assign_uid_res.status_code == 200, assign_uid_res.text
    d3 = assign_uid_res.json()
    assert d3["team_id"] == t3_id
    assert d3["team_name"] == "Alice's Aces"
    assert d3["owner_user_id"] == member.id
    assert d3["owner_name"] == "Member Alice"
    assert d3["is_ai_owned"] is False

    t3_db = db.get(Team, t3_id)
    assert t3_db.owner_user_id == member.id
    assert t3_db.is_ai_owned is False
    assert t3_db.name == "Alice's Aces"
    print(f"[OK] AI Team 3 assigned by user_id to Member Alice (id={member.id})")

    # 6. Simulate roster presence on Team 4 (Gamma AI) and assign mid-season
    roster_entry = RosterEntry(team_id=t4_id, draftable_asset_id=asset.id, acquired_via="draft")
    db.add(roster_entry)
    db.commit()

    assign_t4_res = client.post(
        f"/leagues/{league_id}/teams/{t4_id}/assign",
        headers=commish_headers,
        json={"email": "latemanager@example.com"},
    )
    assert assign_t4_res.status_code == 200, assign_t4_res.text
    assert assign_t4_res.json()["is_ai_owned"] is False
    # Verify roster entry was preserved!
    entries = db.query(RosterEntry).filter(RosterEntry.team_id == t4_id).all()
    assert len(entries) == 1
    assert entries[0].draftable_asset_id == asset.id
    print("[OK] AI Team 4 with active roster entries successfully assigned without losing roster data")

    # 7. Convert Team 3 back to AI
    convert_ai_res = client.post(
        f"/leagues/{league_id}/teams/{t3_id}/assign",
        headers=commish_headers,
        json={"is_ai": True, "team_name": "Beta AI (Restored)"},
    )
    assert convert_ai_res.status_code == 200, convert_ai_res.text
    c_data = convert_ai_res.json()
    assert c_data["is_ai_owned"] is True
    assert c_data["owner_user_id"] is None
    assert c_data["status"] == "converted_to_ai"

    t3_db_after = db.get(Team, t3_id)
    assert t3_db_after.is_ai_owned is True
    assert t3_db_after.owner_user_id is None
    assert t3_db_after.name == "Beta AI (Restored)"
    print("[OK] Team 3 converted back to AI cleanly (ck_team_owner_xor_ai satisfied)")

    # 8. Test GET /leagues/{id}/overview tie-out
    overview_res = client.get(f"/leagues/{league_id}/overview")
    assert overview_res.status_code == 200, overview_res.text
    ov_data = overview_res.json()
    teams_dict = {t["id"]: t for t in ov_data["teams"]}

    assert teams_dict[t1_id]["owner_name"] == "Commissioner Dan"
    assert teams_dict[t2_id]["owner_email"] == "freshmanager@example.com"
    assert teams_dict[t2_id]["is_ai_owned"] is False
    assert teams_dict[t3_id]["is_ai_owned"] is True
    assert teams_dict[t3_id]["owner_user_id"] is None
    assert teams_dict[t4_id]["owner_email"] == "latemanager@example.com"
    assert teams_dict[t4_id]["is_ai_owned"] is False
    print("[OK] GET /leagues/{id}/overview successfully returned owner_email and team ownership statuses")

    db.close()
    engine.dispose()
    if DB_FILE.exists():
        try:
            DB_FILE.unlink()
        except OSError:
            pass

    print("=== All Commissioner AI Team Assignment Checks Passed! ===")


if __name__ == "__main__":
    main()
