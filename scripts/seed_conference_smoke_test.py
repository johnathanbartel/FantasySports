"""
Round-trips conference assignment and program-size mode through the real
HTTP API (FastAPI's TestClient): create a league (gets 3 baseline
conferences, one per program_size tier, for free) -> reject 'dynamic'
program_size_mode as not-yet-implemented -> create teams of each tier
through the new POST /teams endpoint and confirm each lands in the right
conference with the right season_goal -> confirm load balancing places a
second small-program team in the same conference (only one small
conference exists yet) -> expand conferences and confirm it added exactly
one new conference per tier, not a single one.

Uses its own throwaway SQLite file (conference_smoke_test.db) so it never
touches a real dev database. Run from the repo root with the API venv:

    .venv-api/Scripts/python.exe scripts/seed_conference_smoke_test.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["DATABASE_URL"] = "sqlite:///./conference_smoke_test.db"

db_path = Path("conference_smoke_test.db")
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.db.models.core import Universe, User

Base.metadata.create_all(bind=engine)
client = TestClient(app)


def main():
    db = SessionLocal()
    ncaaf = Universe(sport="football", level="college", code="ncaaf", display_name="NCAAF")
    db.add(ncaaf)
    commissioner = User(email="johnathan@example.com", display_name="Johnathan")
    db.add(commissioner)
    db.commit()

    resp = client.post("/sessions", json={"user_id": commissioner.id})
    assert resp.status_code == 200, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['token']}"}

    # --- 'dynamic' mode is rejected until multi-season support exists -----
    resp = client.post("/leagues", json={
        "name": "Dynamic Test League", "commissioner_user_id": commissioner.id,
        "universe_codes": ["ncaaf"], "program_size_mode": "dynamic",
    }, headers=headers)
    print(f"dynamic mode request: {resp.status_code} {resp.json()}")
    assert resp.status_code == 400, "dynamic mode should be rejected for now"

    # --- Create a real league: gets 3 baseline conferences for free -------
    resp = client.post("/leagues", json={
        "name": "Conference Smoke Test League", "commissioner_user_id": commissioner.id,
        "universe_codes": ["ncaaf"], "size": 6, "draft_rounds": 1,
    }, headers=headers)
    assert resp.status_code == 200, resp.text
    league = resp.json()
    print(f"\nCreated league {league['id']}, program_size_mode={league['program_size_mode']}")

    conferences = client.get(f"/leagues/{league['id']}/conferences").json()
    print(f"Baseline conferences: {[(c['program_size'], c['name'], c['team_count']) for c in conferences]}")
    assert len(conferences) == 3, "a new league should start with exactly 3 conferences (one per tier)"
    assert {c["program_size"] for c in conferences} == {"large", "moderate", "small"}
    assert all(c["team_count"] == 0 for c in conferences)

    # --- Create one team per tier through the real create-team endpoint ---
    teams = {}
    for size in ("large", "moderate", "small"):
        resp = client.post("/teams", json={
            "league_id": league["id"], "name": f"{size.title()} Program", "is_ai_owned": True,
            "program_size": size,
        }, headers=headers)
        assert resp.status_code == 200, resp.text
        team = resp.json()
        teams[size] = team
        print(f"Created team {team['id']} ({team['name']}): program_size={team['program_size']}, "
              f"conference_id={team['conference_id']}, season_goal='{team['season_goal']}'")

    assert teams["large"]["season_goal"] == "Win the championship"
    assert teams["moderate"]["season_goal"] == "Make the playoffs"
    assert teams["small"]["season_goal"] == "Win the conference"

    large_conf_id = next(c["id"] for c in conferences if c["program_size"] == "large")
    assert teams["large"]["conference_id"] == large_conf_id, "the large team should land in the large conference"

    # A team-creation with an inconsistent owner/AI combination should fail,
    # matching the same XOR rule enforced at the DB layer for direct inserts.
    resp = client.post("/teams", json={
        "league_id": league["id"], "name": "Bad Team", "is_ai_owned": True,
        "owner_user_id": commissioner.id, "program_size": "small",
    }, headers=headers)
    print(f"\nInconsistent owner/AI request: {resp.status_code} {resp.json()}")
    assert resp.status_code == 400

    # --- Load balancing: a 2nd small-program team joins the same (only) conference ---
    resp = client.post("/teams", json={
        "league_id": league["id"], "name": "Small Program 2", "is_ai_owned": True, "program_size": "small",
    }, headers=headers)
    assert resp.status_code == 200, resp.text
    small2 = resp.json()
    print(f"\n2nd small-program team {small2['id']} conference_id={small2['conference_id']} "
          f"(should match the first small team's {teams['small']['conference_id']})")
    assert small2["conference_id"] == teams["small"]["conference_id"]

    conferences_after = {c["id"]: c for c in client.get(f"/leagues/{league['id']}/conferences").json()}
    assert conferences_after[teams["small"]["conference_id"]]["team_count"] == 2

    # --- Expanding conferences adds one per tier, never just one ----------
    resp = client.post(f"/leagues/{league['id']}/conferences/expand")
    assert resp.status_code == 200, resp.text
    expanded = resp.json()
    print(f"\nExpand result: {[(c['program_size'], c['name']) for c in expanded]}")
    assert len(expanded) == 3, "expanding must add one conference per tier, never a single one"

    all_conferences = client.get(f"/leagues/{league['id']}/conferences").json()
    assert len(all_conferences) == 6, "3 baseline + 3 from one expansion = 6"
    for size in ("large", "moderate", "small"):
        count = sum(1 for c in all_conferences if c["program_size"] == size)
        assert count == 2, f"{size} should now have exactly 2 conferences"
    print(f"Total conferences after one expansion: {len(all_conferences)} (2 per tier, balanced)")

    # --- A 3rd small-program team should now load-balance into the NEW small conference ---
    resp = client.post("/teams", json={
        "league_id": league["id"], "name": "Small Program 3", "is_ai_owned": True, "program_size": "small",
    }, headers=headers)
    assert resp.status_code == 200, resp.text
    small3 = resp.json()
    print(f"3rd small-program team {small3['id']} conference_id={small3['conference_id']} "
          f"(should differ from the first two, which are in the fuller conference)")
    assert small3["conference_id"] != teams["small"]["conference_id"]

    print("\nConference smoke test passed: baseline conferences, tier-balanced team placement, "
          "season goals, dynamic-mode rejection, and tier-balanced expansion all round-tripped through the API.")


if __name__ == "__main__":
    main()
