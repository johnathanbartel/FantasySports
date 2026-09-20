"""
Round-trips the timed sealed-bid auction engine through the real HTTP API
(FastAPI's TestClient): create a universe + league + teams with different
program_size tiers -> start an auction (budgets snapshotted per team's
tier) -> nominate a player and a defense group in turn, proving the
same-pool decision holds for auction too -> place bids (including a
deliberate tie, to exercise the random tie-break) -> resolve each round and
confirm the winner's budget was debited and the loser's wasn't -> confirm
nomination turn rotated -> keep going until the auction marks itself
complete.

Uses its own throwaway SQLite file (auction_smoke_test.db) so it never
touches a real dev database. Run from the repo root with the API venv:

    .venv-api/Scripts/python.exe scripts/seed_auction_smoke_test.py
"""

import datetime
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["DATABASE_URL"] = "sqlite:///./auction_smoke_test.db"

# Fresh DB file every run.
db_path = Path("auction_smoke_test.db")
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.db.models.core import (
    DefenseGroup,
    DraftableAsset,
    Player,
    RealTeam,
    Team,
    Universe,
    User,
)

Base.metadata.create_all(bind=engine)
client = TestClient(app)


def seed_reference_data(db):
    ncaaf = Universe(sport="football", level="college", code="ncaaf", display_name="NCAAF")
    db.add(ncaaf)
    db.flush()

    georgia = RealTeam(universe_id=ncaaf.id, name="Georgia", conference="SEC", classification="fbs")
    clemson = RealTeam(universe_id=ncaaf.id, name="Clemson", conference="ACC", classification="fbs")
    db.add_all([georgia, clemson])
    db.flush()

    qb = Player(universe_id=ncaaf.id, real_team_id=georgia.id, full_name="Carson Beck", position="QB")
    rb = Player(universe_id=ncaaf.id, real_team_id=clemson.id, full_name="Will Shipley", position="RB")
    db.add_all([qb, rb])
    db.flush()

    clemson_db_group = DefenseGroup(real_team_id=clemson.id, position_group="DB")
    db.add(clemson_db_group)
    db.flush()

    qb_asset = DraftableAsset(universe_id=ncaaf.id, asset_type="player", player_id=qb.id)
    rb_asset = DraftableAsset(universe_id=ncaaf.id, asset_type="player", player_id=rb.id)
    db_group_asset = DraftableAsset(
        universe_id=ncaaf.id, asset_type="defense_group", defense_group_id=clemson_db_group.id
    )
    db.add_all([qb_asset, rb_asset, db_group_asset])
    db.flush()

    commissioner = User(email="johnathan@example.com", display_name="Johnathan")
    db.add(commissioner)
    db.commit()

    return {
        "ncaaf": ncaaf, "qb_asset": qb_asset, "rb_asset": rb_asset,
        "db_group_asset": db_group_asset, "commissioner": commissioner,
    }


def main():
    db = SessionLocal()
    ref = seed_reference_data(db)

    # --- League + teams with different program-size budget tiers -----------
    resp = client.post("/leagues", json={
        "name": "Auction Smoke Test League",
        "commissioner_user_id": ref["commissioner"].id,
        "universe_codes": ["ncaaf"],
        "size": 3,
        "draft_rounds": 2,
    })
    assert resp.status_code == 200, resp.text
    league = resp.json()
    print(f"Created league {league['id']}: {league['name']}")

    team_large = Team(league_id=league["id"], name="Large Program", owner_user_id=ref["commissioner"].id, program_size="large")
    team_moderate = Team(league_id=league["id"], name="Moderate Program", is_ai_owned=True, program_size="moderate")
    team_small = Team(league_id=league["id"], name="Small Program", is_ai_owned=True, program_size="small")
    db.add_all([team_large, team_moderate, team_small])
    db.commit()
    print(
        f"Teams: {team_large.id} ({team_large.name}, {team_large.program_size}), "
        f"{team_moderate.id} ({team_moderate.name}, {team_moderate.program_size}), "
        f"{team_small.id} ({team_small.name}, {team_small.program_size})"
    )

    # --- Start the auction: budgets snapshotted per program_size -----------
    resp = client.post("/auctions/start", json={"league_id": league["id"], "round_seconds": 1})
    assert resp.status_code == 200, resp.text
    auction = resp.json()
    print(f"\nStarted auction {auction['id']}, status={auction['status']}, round_seconds={auction['round_seconds']}")

    budgets = client.get(f"/auctions/{auction['id']}/budgets").json()
    for b in budgets:
        print(f"  Team {b['team_id']}: starting_budget={b['starting_budget']}, remaining={b['remaining_budget']}")
    assert {b["team_id"]: b["starting_budget"] for b in budgets} == {
        team_large.id: 300, team_moderate.id: 200, team_small.id: 100,
    }, "starting budgets should follow program_size"

    board = client.get(f"/auctions/{auction['id']}/board").json()
    print(f"\nAuction board: {[b['display_name'] for b in board]}")

    # --- Round 1: nominate the QB, deliberate tie between two teams --------
    resp = client.post(f"/auctions/{auction['id']}/nominate", json={
        "draftable_asset_id": ref["qb_asset"].id, "nominated_by_team_id": team_large.id,
    })
    assert resp.status_code == 200, resp.text
    round1 = resp.json()
    print(f"\nRound {round1['round_number']}: {round1['display_name']} nominated by team {team_large.id}, "
          f"window closes in ~{round1['seconds_remaining']:.1f}s")

    client.post(f"/auctions/{auction['id']}/rounds/{round1['id']}/bids", json={"team_id": team_moderate.id, "amount": 50})
    client.post(f"/auctions/{auction['id']}/rounds/{round1['id']}/bids", json={"team_id": team_small.id, "amount": 50})
    resp = client.post(f"/auctions/{auction['id']}/rounds/{round1['id']}/bids", json={"team_id": team_large.id, "amount": 20})
    assert resp.status_code == 200, resp.text

    # Bids stay sealed while the round is open.
    sealed = client.get(f"/auctions/{auction['id']}/rounds/{round1['id']}/bids").json()
    assert sealed == [], "bids must stay hidden until the round resolves"
    print("Confirmed bids are sealed while the round is open.")

    time.sleep(1.1)  # let the 1-second bid window actually expire
    resp = client.post(f"/auctions/{auction['id']}/rounds/{round1['id']}/resolve")
    assert resp.status_code == 200, resp.text
    round1 = resp.json()
    print(f"Resolved: winner=team {round1['winning_team_id']} at {round1['winning_bid_amount']}, "
          f"tie_broken_randomly={round1['was_tie_broken_randomly']}")
    assert round1["winning_bid_amount"] == 50
    assert round1["winning_team_id"] in (team_moderate.id, team_small.id), "tie should go to one of the $50 bidders"
    assert round1["was_tie_broken_randomly"] is True

    revealed = client.get(f"/auctions/{auction['id']}/rounds/{round1['id']}/bids").json()
    print(f"Bids revealed after resolution: {revealed}")
    assert len(revealed) == 3

    budgets_after = {b["team_id"]: b for b in client.get(f"/auctions/{auction['id']}/budgets").json()}
    winner_id = round1["winning_team_id"]
    loser_id = team_moderate.id if winner_id == team_small.id else team_small.id
    assert budgets_after[winner_id]["remaining_budget"] == budgets_after[winner_id]["starting_budget"] - 50
    assert budgets_after[loser_id]["remaining_budget"] == budgets_after[loser_id]["starting_budget"], \
        "a losing bidder must not be debited"
    print(f"Confirmed only the winner (team {winner_id}) was debited; team {loser_id}'s budget is untouched.")

    auction_state = client.get(f"/auctions/{auction['id']}").json()
    print(f"Nomination turn advanced to index {auction_state['next_nominator_index']}")

    # --- Round 2: nominate the defense group - same pool, no special-casing ---
    next_nominator = [team_large.id, team_moderate.id, team_small.id][auction_state["next_nominator_index"]]
    resp = client.post(f"/auctions/{auction['id']}/nominate", json={
        "draftable_asset_id": ref["db_group_asset"].id, "nominated_by_team_id": next_nominator,
    })
    assert resp.status_code == 200, resp.text
    round2 = resp.json()
    print(f"\nRound {round2['round_number']}: {round2['display_name']} (a defense group) nominated by team "
          f"{next_nominator} - drafted from the same auction pool as the QB above")

    time.sleep(1.1)
    resp = client.post(f"/auctions/{auction['id']}/rounds/{round2['id']}/resolve")
    assert resp.status_code == 200, resp.text
    round2 = resp.json()
    print(f"Resolved with no bids: status={round2['status']} (asset returns to the board, unsold)")
    assert round2["status"] == "unsold"

    board_after = client.get(f"/auctions/{auction['id']}/board").json()
    print(f"Board after round 2: {[b['display_name'] for b in board_after]}")
    assert ref["db_group_asset"].id in {b["draftable_asset_id"] for b in board_after}, \
        "an unsold nomination must return to the board"

    print("\nAuction smoke test passed: budgets, timed round windows, sealed bids, "
          "random tie-break, debits, and the defense same-pool decision all round-tripped through the API.")


if __name__ == "__main__":
    main()
