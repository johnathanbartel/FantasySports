"""
Round-trips real data through the schema and the API layer together (via
FastAPI's TestClient, so this exercises the actual HTTP routes, not just
raw DB session calls): create a universe + league + teams -> run a snake
draft covering both a player and a defense group (proving the same-pool
recommendation works) -> set a week-1 lineup -> insert a couple of plays
and play_credits shaped like what sports/football/college/pipeline.py already
produces -> roll up to a weekly player/team score -> read it back out
through the API.

Uses its own throwaway SQLite file (smoke_test.db) so it never touches a
real dev database. Run from the repo root with the API venv:

    .venv-api/Scripts/python.exe scripts/seed_smoke_test.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["DATABASE_URL"] = "sqlite:///./smoke_test.db"

# Fresh DB file every run.
db_path = Path("smoke_test.db")
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.db.models.core import (
    DefenseGroup,
    DraftableAsset,
    Game,
    LeagueUniverse,
    Player,
    RealTeam,
    RosterSlotDefinition,
    Team,
    Universe,
    User,
    Week,
)
from fantasy_sports.sports.football.models import Play, PlayCredit, WeeklyPlayerScore

Base.metadata.create_all(bind=engine)
client = TestClient(app)


def seed_reference_data(db):
    """Reference/admin data - not something a user action creates."""
    ncaaf = Universe(sport="football", level="college", code="ncaaf", display_name="NCAAF")
    db.add(ncaaf)
    db.flush()

    georgia = RealTeam(universe_id=ncaaf.id, name="Georgia", conference="SEC", classification="fbs")
    clemson = RealTeam(universe_id=ncaaf.id, name="Clemson", conference="ACC", classification="fbs")
    db.add_all([georgia, clemson])
    db.flush()

    qb = Player(universe_id=ncaaf.id, real_team_id=georgia.id, full_name="Carson Beck", position="QB")
    db.add(qb)
    db.flush()

    clemson_db_group = DefenseGroup(real_team_id=clemson.id, position_group="DB")
    db.add(clemson_db_group)
    db.flush()

    qb_asset = DraftableAsset(universe_id=ncaaf.id, asset_type="player", player_id=qb.id)
    db_group_asset = DraftableAsset(
        universe_id=ncaaf.id, asset_type="defense_group", defense_group_id=clemson_db_group.id
    )
    db.add_all([qb_asset, db_group_asset])
    db.flush()

    slot = RosterSlotDefinition(
        universe_id=ncaaf.id, slot_code="QB", eligible_positions=["QB"], slot_count=1, is_starter=True
    )
    db.add(slot)

    week1 = Week(universe_id=ncaaf.id, season_year=2024, week_number=1)
    db.add(week1)
    db.flush()

    game = Game(
        universe_id=ncaaf.id, week_id=week1.id, home_team_id=georgia.id, away_team_id=clemson.id,
        home_score=34, away_score=3, status="final",
    )
    db.add(game)
    db.flush()

    commissioner = User(email="johnathan@example.com", display_name="Johnathan")
    db.add(commissioner)
    db.commit()

    return {
        "ncaaf": ncaaf, "qb_asset": qb_asset, "db_group_asset": db_group_asset,
        "slot": slot, "week1": week1, "game": game, "commissioner": commissioner,
    }


def main():
    db = SessionLocal()
    ref = seed_reference_data(db)

    # --- League + teams -----------------------------------------------
    resp = client.post("/leagues", json={
        "name": "Smoke Test League",
        "commissioner_user_id": ref["commissioner"].id,
        "universe_codes": ["ncaaf"],
        "size": 2,
        "draft_rounds": 2,
    })
    assert resp.status_code == 200, resp.text
    league = resp.json()
    print(f"Created league {league['id']}: {league['name']}")

    team_a = Team(league_id=league["id"], name="Team A", owner_user_id=ref["commissioner"].id)
    team_b = Team(league_id=league["id"], name="Team B", is_ai_owned=True)
    db.add_all([team_a, team_b])
    db.commit()
    print(f"Teams: {team_a.id} ({team_a.name}), {team_b.id} ({team_b.name}, AI-owned)")

    # --- Draft: covers both a player and a defense group ---------------
    resp = client.post("/drafts/start", json={"league_id": league["id"]})
    assert resp.status_code == 200, resp.text
    draft = resp.json()
    print(f"Started draft {draft['id']}, status={draft['status']}")

    board = client.get(f"/drafts/{draft['id']}/board").json()
    print(f"Draft board: {[b['display_name'] for b in board]}")

    resp = client.post(f"/drafts/{draft['id']}/pick", json={"draftable_asset_id": ref["qb_asset"].id})
    assert resp.status_code == 200, resp.text
    print(f"Pick 1: {resp.json()['display_name']} -> team {resp.json()['team_id']}")

    resp = client.post(
        f"/drafts/{draft['id']}/pick", json={"draftable_asset_id": ref["db_group_asset"].id}
    )
    assert resp.status_code == 200, resp.text
    print(f"Pick 2: {resp.json()['display_name']} -> team {resp.json()['team_id']} "
          f"(a defense group, drafted from the same pool as the QB above)")

    # Fill remaining picks via autopick.
    for _ in range(2):
        resp = client.post(f"/drafts/{draft['id']}/autopick")
        if resp.status_code == 200:
            print(f"Autopick: {resp.json()['display_name']} -> team {resp.json()['team_id']}")

    picks = client.get(f"/drafts/{draft['id']}/picks").json()
    print(f"Full pick history ({len(picks)} picks): "
          f"{[(p['round'], p['pick_number'], p['display_name']) for p in picks]}")

    # --- Lineup ----------------------------------------------------------
    resp = client.put(f"/teams/{team_a.id}/lineup", json={
        "week_id": ref["week1"].id,
        "assignments": {"QB": ref["qb_asset"].id},
    })
    assert resp.status_code == 200, resp.text
    print(f"Set week-1 lineup for team {team_a.id}: {resp.json()}")

    # --- Fake plays + play_credits, shaped like pipeline.py's real output ---
    play1 = Play(
        game_id=ref["game"].id, source="cfbd", external_play_id="smoke-1",
        down=1, distance=10, yards_to_goal=75, offense_team_id=None,
        play_type="Pass Reception", play_text="Carson Beck pass complete to Arian Smith for 11 yds",
        yards_gained=11, ppa=1.059,
    )
    play2 = Play(
        game_id=ref["game"].id, source="cfbd", external_play_id="smoke-2",
        down=2, distance=8, yards_to_goal=64, offense_team_id=None,
        play_type="Rush", play_text="Carson Beck run for 13 yds", yards_gained=13, ppa=3.060,
    )
    db.add_all([play1, play2])
    db.flush()

    db.add_all([
        PlayCredit(play_id=play1.id, draftable_asset_id=ref["qb_asset"].id, role="passer", credit_points=0.244),
        PlayCredit(play_id=play2.id, draftable_asset_id=ref["qb_asset"].id, role="rusher", credit_points=3.060),
    ])
    db.commit()

    total_points = 0.244 + 3.060
    db.add(WeeklyPlayerScore(
        draftable_asset_id=ref["qb_asset"].id, week_id=ref["week1"].id, points=total_points,
    ))
    db.commit()

    # --- Read it back through the API -------------------------------------
    resp = client.get(
        f"/players/{ref['qb_asset'].id}/weekly-score", params={"week_id": ref["week1"].id}
    )
    assert resp.status_code == 200, resp.text
    breakdown = resp.json()
    print(f"\nWeekly score breakdown for {breakdown['display_name']}: "
          f"{breakdown['total_points']:.3f} points")
    for pc in breakdown["play_credits"]:
        print(f"  {pc['credit_points']:+.3f}  {pc['role']:<8} {pc['play_text']}")

    resp = client.get(f"/teams/{team_a.id}/weekly-score", params={"week_id": ref["week1"].id})
    assert resp.status_code == 200, resp.text
    print(f"\nTeam {team_a.id} week-1 total: {resp.json()['total_points']:.3f}")

    resp = client.get(f"/leagues/{league['id']}/standings")
    assert resp.status_code == 200, resp.text
    print(f"\nStandings (no matchups played yet, so all zero): {resp.json()}")

    print("\nSmoke test passed: schema + API round-tripped real data end to end.")


if __name__ == "__main__":
    main()
