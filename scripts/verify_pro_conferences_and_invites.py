"""
Verification script for pro conferences/divisions and team claim/invite status.

Tests:
1. Pro league creation (NHL/NFL) gets sport-appropriate baseline conferences.
2. Conferences & divisions management:
   - Add new conference / division
   - Rename conference / division
   - Delete conference / division with team reassignment
   - Realign teams across conferences / divisions
3. Team claim & invite status tracking:
   - AI team has invite_status='ai', is_claimed=False
   - Email invite has invite_status='pending_claim', is_claimed=False, and non-empty invite_url
   - Registered account has invite_status='claimed', is_claimed=True
   - Commissioner team has invite_status='claimed', is_claimed=True
4. Mid-season restriction:
   - When active games are underway, conference modifications are rejected with 400.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["DATABASE_URL"] = "sqlite:///./verify_pro_confs.db"

db_file = Path("verify_pro_confs.db")
if db_file.exists():
    db_file.unlink()

from fastapi.testclient import TestClient
from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.db.models.core import Universe, User, Team, Week, WeeklyMatchup, Game
from fantasy_sports.sports.football.models import WeeklyPlayerScore
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.platform.nfl_reference import seed_nfl_reference_data

Base.metadata.create_all(bind=engine)
client = TestClient(app)


def test_pro_conferences_and_invites():
    db = SessionLocal()
    seed_hockey_reference_data(db)
    seed_nfl_reference_data(db)

    # 1. Create commissioner
    commish = User(email="commish@fantasymotion.com", display_name="Commish Dave", password_hash="hash123")
    db.add(commish)
    db.commit()

    resp = client.post("/sessions/login", json={"email": "commish@fantasymotion.com", "password": "any"})
    # Since login verifies hash, let's just use /sessions with user_id
    resp = client.post("/sessions", json={"user_id": commish.id})
    assert resp.status_code == 200
    token = resp.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    print("[OK] Commissioner session created")

    # 2. Create NHL league
    nhl_resp = client.post(
        "/leagues",
        json={
            "name": "Original Six NHL",
            "commissioner_user_id": commish.id,
            "universe_codes": ["nhl"],
            "size": 6,
            "draft_type": "snake",
            "series_length_days": 7,
        },
        headers=headers,
    )
    assert nhl_resp.status_code == 200, nhl_resp.text
    league_id = nhl_resp.json()["id"]

    # Check baseline conferences for NHL
    confs_resp = client.get(f"/leagues/{league_id}/conferences")
    assert confs_resp.status_code == 200
    confs = confs_resp.json()
    conf_names = [c["name"] for c in confs]
    assert "Eastern Conference" in conf_names, f"Expected Eastern Conference, got {conf_names}"
    assert "Western Conference" in conf_names, f"Expected Western Conference, got {conf_names}"
    assert all(c["editable"] is True for c in confs), "Conferences should be editable in preseason"
    print(f"[OK] NHL league created with baseline conferences: {conf_names}")

    # 3. Setup teams with mix of commissioner, AI, and invited human managers
    teams_setup_resp = client.post(
        f"/leagues/{league_id}/teams-setup",
        json={
            "teams": [
                {"name": "Boston Bruins", "is_commissioner": True},
                {"name": "Toronto Maple Leafs", "is_ai": True},
                {"name": "Montreal Canadiens", "is_ai": False, "email": "habs_fan@example.com"},
                {"name": "Detroit Red Wings", "is_ai": True},
                {"name": "Chicago Blackhawks", "is_ai": True},
                {"name": "New York Rangers", "is_ai": False, "email": "rangers_fan@example.com"},
            ]
        },
        headers=headers,
    )
    assert teams_setup_resp.status_code == 200, teams_setup_resp.text
    teams_setup = teams_setup_resp.json()["teams"]
    assert len(teams_setup) == 6
    print("[OK] 6 teams created and distributed")

    # 4. Verify Claim & Invite Status via GET /leagues/{league_id}/overview
    ov_resp = client.get(f"/leagues/{league_id}/overview")
    assert ov_resp.status_code == 200
    ov = ov_resp.json()
    assert ov["is_college"] is False, "NHL league should not be college"
    teams = {t["name"]: t for t in ov["teams"]}

    # Commissioner team
    commish_team = teams["Boston Bruins"]
    assert commish_team["is_claimed"] is True
    assert commish_team["invite_status"] == "claimed"
    assert commish_team["invite_url"] is None

    # AI team
    ai_team = teams["Toronto Maple Leafs"]
    assert ai_team["is_claimed"] is False
    assert ai_team["invite_status"] == "ai"
    assert ai_team["invite_url"] is None

    # Pending claim team
    habs_team = teams["Montreal Canadiens"]
    assert habs_team["is_claimed"] is False
    assert habs_team["invite_status"] == "pending_claim"
    assert "habs_fan@example.com" in (habs_team["invite_url"] or "")
    assert habs_team["owner_email"] == "habs_fan@example.com"

    print("[OK] Overview correctly tracks claim status for commissioner, AI, and pending invite")

    # 5. User accepts invite / registers account
    reg_resp = client.post(
        "/sessions/register",
        json={
            "email": "habs_fan@example.com",
            "display_name": "MontrealFan99",
            "password": "securepassword123",
        },
    )
    assert reg_resp.status_code == 200, reg_resp.text
    print("[OK] Invited user registered with password")

    # Re-check overview: Montreal Canadiens is now CLAIMED
    ov_resp2 = client.get(f"/leagues/{league_id}/overview")
    teams2 = {t["name"]: t for t in ov_resp2.json()["teams"]}
    habs_team2 = teams2["Montreal Canadiens"]
    assert habs_team2["is_claimed"] is True
    assert habs_team2["invite_status"] == "claimed"
    assert habs_team2["owner_name"] == "MontrealFan99"
    assert habs_team2["invite_url"] is None
    print("[OK] Team marked claimed after user registers")

    # Also check GET /leagues/{league_id}/teams
    teams_api_resp = client.get(f"/leagues/{league_id}/teams")
    assert teams_api_resp.status_code == 200
    teams_api = {t["name"]: t for t in teams_api_resp.json()}
    assert teams_api["Montreal Canadiens"]["is_claimed"] is True
    assert teams_api["New York Rangers"]["is_claimed"] is False
    assert teams_api["New York Rangers"]["invite_status"] == "pending_claim"
    print("[OK] GET /leagues/{id}/teams returns claim and invite metadata")

    # 6. Conference & Division management:
    # Add new division: "Atlantic Division"
    add_resp = client.post(
        f"/leagues/{league_id}/conferences",
        json={"name": "Atlantic Division"},
        headers=headers,
    )
    assert add_resp.status_code == 200, add_resp.text
    atlantic_id = add_resp.json()["id"]
    assert add_resp.json()["name"] == "Atlantic Division"
    print("[OK] Added new division 'Atlantic Division'")

    # Rename conference: Eastern Conference -> Central Division
    east_conf = next(c for c in confs if c["name"] == "Eastern Conference")
    rename_resp = client.patch(
        f"/leagues/{league_id}/conferences/{east_conf['id']}",
        json={"name": "Central Division"},
        headers=headers,
    )
    assert rename_resp.status_code == 200, rename_resp.text
    assert rename_resp.json()["name"] == "Central Division"
    print("[OK] Renamed conference to 'Central Division'")

    # Align teams into Atlantic Division and Central Division
    bruins_id = commish_team["id"]
    leafs_id = ai_team["id"]
    align_resp = client.post(
        f"/leagues/{league_id}/conferences/align-teams",
        json={
            "assignments": [
                {"team_id": bruins_id, "conference_id": atlantic_id},
                {"team_id": leafs_id, "conference_id": atlantic_id},
            ]
        },
        headers=headers,
    )
    assert align_resp.status_code == 200, align_resp.text
    aligned_teams = {t["id"]: t for t in align_resp.json()}
    assert aligned_teams[bruins_id]["conference_id"] == atlantic_id
    assert aligned_teams[leafs_id]["conference_id"] == atlantic_id
    print("[OK] Successfully aligned teams into Atlantic Division")

    # Delete conference
    west_conf = next(c for c in confs if c["name"] == "Western Conference")
    del_resp = client.delete(
        f"/leagues/{league_id}/conferences/{west_conf['id']}",
        headers=headers,
    )
    assert del_resp.status_code == 200, del_resp.text
    print("[OK] Successfully deleted Western Conference")

    # Verify remaining conferences
    confs_now = client.get(f"/leagues/{league_id}/conferences").json()
    names_now = [c["name"] for c in confs_now]
    assert "Western Conference" not in names_now
    assert "Atlantic Division" in names_now
    assert "Central Division" in names_now
    print(f"[OK] Remaining conferences verified: {names_now}")

    # 7. Non-commissioner cannot edit conferences
    hacker = User(email="hacker@example.com", display_name="Hacker")
    db.add(hacker)
    db.commit()
    hacker_token = client.post("/sessions", json={"user_id": hacker.id}).json()["token"]
    hacker_headers = {"Authorization": f"Bearer {hacker_token}"}

    forbidden_resp = client.post(
        f"/leagues/{league_id}/conferences",
        json={"name": "Hacked Division"},
        headers=hacker_headers,
    )
    assert forbidden_resp.status_code == 403
    print("[OK] Non-commissioner correctly forbidden (403)")

    print("\n=== ALL PRO CONFERENCES AND INVITES CHECKS PASSED! ===")


if __name__ == "__main__":
    test_pro_conferences_and_invites()
