r"""
Verification script for conferences and divisions hierarchy, structure configuration,
and even randomized assignment.

Run with:
    .\.venv-api\Scripts\python.exe scripts/verify_conference_division_randomize.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["DATABASE_URL"] = "sqlite:///./verify_conf_divs.db"

db_file = Path("verify_conf_divs.db")
if db_file.exists():
    db_file.unlink()

from fastapi.testclient import TestClient
from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.db.models.core import Universe, User, Team, Conference, Division
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.platform.league_lifecycle import delete_league

Base.metadata.create_all(bind=engine)
client = TestClient(app)


def check(cond, msg):
    if not cond:
        print(f"[FAIL] {msg}")
        raise AssertionError(msg)
    print(f"[OK] {msg}")


def test_conferences_and_divisions():
    db = SessionLocal()
    seed_hockey_reference_data(db)

    # 1. Create commissioner
    commish = User(email="commish@example.com", display_name="Commish Alice", password_hash="hash")
    db.add(commish)
    db.commit()

    resp = client.post("/sessions", json={"user_id": commish.id})
    check(resp.status_code == 200, "Commissioner session created")
    token = resp.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 2. Create NHL league with 10 teams
    create_resp = client.post(
        "/leagues",
        json={
            "name": "Hierarchy NHL League",
            "commissioner_user_id": commish.id,
            "universe_codes": ["nhl"],
            "size": 10,
            "draft_type": "snake",
            "series_length_days": 7,
        },
        headers=headers,
    )
    check(create_resp.status_code == 200, "League created")
    league_id = create_resp.json()["id"]

    # Fill with 10 AI teams
    setup_teams = [
        {"name": f"Team {i+1}", "is_commissioner": (i == 0), "is_ai": (i > 0)}
        for i in range(10)
    ]
    t_resp = client.post(f"/leagues/{league_id}/teams-setup", json={"teams": setup_teams}, headers=headers)
    check(t_resp.status_code == 200, "10 teams configured")

    # 3. Configure Structure: 2 Conferences, 2 Divisions each (4 divisions total)
    setup_struct_resp = client.post(
        f"/leagues/{league_id}/conferences/setup-structure",
        json={
            "conference_count": 2,
            "divisions_per_conference": 2,
            "randomize_teams": True,
        },
        headers=headers,
    )
    check(setup_struct_resp.status_code == 200, "Structure setup 2 confs x 2 divs succeeded")
    confs = setup_struct_resp.json()
    check(len(confs) == 2, f"Expected 2 conferences, got {len(confs)}")
    check(confs[0]["name"] == "Eastern Conference", f"Expected Eastern Conference, got {confs[0]['name']}")
    check(confs[1]["name"] == "Western Conference", f"Expected Western Conference, got {confs[1]['name']}")

    east_divs = confs[0]["divisions"]
    west_divs = confs[1]["divisions"]
    check(len(east_divs) == 2, f"Expected 2 east divs, got {len(east_divs)}")
    check(len(west_divs) == 2, f"Expected 2 west divs, got {len(west_divs)}")
    check(east_divs[0]["name"] == "Atlantic Division", f"Expected Atlantic Division, got {east_divs[0]['name']}")
    check(east_divs[1]["name"] == "Metropolitan Division", f"Expected Metropolitan Division, got {east_divs[1]['name']}")
    check(west_divs[0]["name"] == "Central Division", f"Expected Central Division, got {west_divs[0]['name']}")
    check(west_divs[1]["name"] == "Pacific Division", f"Expected Pacific Division, got {west_divs[1]['name']}")

    # 4. Verify Randomize assigned teams strictly evenly
    # 10 teams into 2 confs with 2 divs each:
    # Each conf should have exactly 5 teams!
    # Each div should have either 2 or 3 teams! (max - min <= 1)
    conf_counts = [c["team_count"] for c in confs]
    check(conf_counts == [5, 5], f"Expected conf counts [5, 5], got {conf_counts}")

    all_div_counts = [d["team_count"] for c in confs for d in c["divisions"]]
    check(max(all_div_counts) - min(all_div_counts) <= 1, f"Divisions not evenly distributed: {all_div_counts}")
    check(sum(all_div_counts) == 10, f"Sum of div counts is {sum(all_div_counts)}, expected 10")
    print(f"[OK] Even distribution verified for 10 teams: confs={conf_counts}, divs={all_div_counts}")

    # 5. Test standalone Randomize endpoint
    rand_resp = client.post(f"/leagues/{league_id}/conferences/randomize", headers=headers)
    check(rand_resp.status_code == 200, "Randomize endpoint succeeded")
    confs_after_rand = client.get(f"/leagues/{league_id}/conferences").json()
    all_div_counts_rand = [d["team_count"] for c in confs_after_rand for d in c["divisions"]]
    check(max(all_div_counts_rand) - min(all_div_counts_rand) <= 1, f"Standalone randomize uneven: {all_div_counts_rand}")
    check(sum(all_div_counts_rand) == 10, "Total teams matches 10 after standalone randomize")

    # 6. Test GET /leagues/{league_id}/teams returns division and conference metadata
    teams_resp = client.get(f"/leagues/{league_id}/teams")
    check(teams_resp.status_code == 200, "GET teams succeeded")
    teams = teams_resp.json()
    check(all(t["division_id"] is not None for t in teams), "All teams have division_id set")
    check(all(t["division_name"] is not None for t in teams), "All teams have division_name set")
    check(all(t["conference_name"] in ["Eastern Conference", "Western Conference"] for t in teams), "All teams have valid conference_name")

    # 7. Test Overview endpoint returns division info
    ov_resp = client.get(f"/leagues/{league_id}/overview")
    check(ov_resp.status_code == 200, "Overview succeeded")
    ov_teams = ov_resp.json()["teams"]
    check(all(t["division_id"] is not None for t in ov_teams), "Overview teams have division_id")
    check(all(t["division_name"] is not None for t in ov_teams), "Overview teams have division_name")

    # 8. Test adding a division to a conference
    east_conf_id = confs[0]["id"]
    add_div_resp = client.post(
        f"/leagues/{league_id}/conferences/{east_conf_id}/divisions",
        json={"name": "Northeast Division"},
        headers=headers,
    )
    check(add_div_resp.status_code == 200, "Added Northeast Division to East Conference")
    ne_div_id = add_div_resp.json()["id"]

    # 9. Test renaming a division
    rename_div_resp = client.patch(
        f"/leagues/{league_id}/divisions/{ne_div_id}",
        json={"name": "New England Division"},
        headers=headers,
    )
    check(rename_div_resp.status_code == 200, "Renamed division")
    check(rename_div_resp.json()["name"] == "New England Division", "Division name updated")

    # 10. Test manual alignment to a division
    team_1 = teams[0]
    align_resp = client.post(
        f"/leagues/{league_id}/conferences/align-teams",
        json={"assignments": [{"team_id": team_1["id"], "division_id": ne_div_id}]},
        headers=headers,
    )
    check(align_resp.status_code == 200, "Aligned team 1 to New England Division")
    t1_updated = next(t for t in align_resp.json() if t["id"] == team_1["id"])
    check(t1_updated["division_id"] == ne_div_id, "Team division_id updated")
    check(t1_updated["conference_id"] == east_conf_id, "Team conference_id automatically synced with division's conference")

    # 11. Test deleting a division
    del_div_resp = client.delete(f"/leagues/{league_id}/divisions/{ne_div_id}", headers=headers)
    check(del_div_resp.status_code == 200, "Deleted New England Division")
    t1_after_del = next(t for t in client.get(f"/leagues/{league_id}/teams").json() if t["id"] == team_1["id"])
    check(t1_after_del["division_id"] is None, "Team division_id cleared after division deleted")
    check(t1_after_del["conference_id"] == east_conf_id, "Team remains in parent conference")

    # 12. Test deleting league cleans up divisions without foreign key errors
    db_session = SessionLocal()
    delete_league(db_session, league_id)
    db_session.commit()
    check(db_session.query(Division).filter_by(league_id=league_id).count() == 0, "All divisions removed on league delete")
    check(db_session.query(Conference).filter_by(league_id=league_id).count() == 0, "All conferences removed on league delete")
    db_session.close()

    print("\nALL CONFERENCES AND DIVISIONS TESTS PASSED!")
    engine.dispose()
    try:
        if db_file.exists():
            db_file.unlink()
    except PermissionError:
        pass


if __name__ == "__main__":
    test_conferences_and_divisions()
