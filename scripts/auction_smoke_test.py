"""
Runs a complete Timed Auction Draft through the real HTTP API (FastAPI's
TestClient, so these are the actual routes the draft board calls, not raw
session work): seed a universe + league + teams -> open a Recruiting Period
with tiered budgets -> for each per-slot round, submit blind bids, reveal,
watch the winners get debited -> read the finished rosters back out.

Companion to scripts/seed_smoke_test.py, which covers the snake-draft path.

Uses its own throwaway SQLite file (auction_smoke_test.db) so it never
touches a real dev database. Run from the repo root with the API venv:

    .venv-api/Scripts/python.exe scripts/auction_smoke_test.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["DATABASE_URL"] = "sqlite:///./auction_smoke_test.db"

# Fresh DB file every run.
db_path = Path("auction_smoke_test.db")
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient  # noqa: E402

from fantasy_sports.api.main import app  # noqa: E402
from fantasy_sports.platform.db.base import Base  # noqa: E402
from fantasy_sports.platform.db.models.core import (  # noqa: E402
    DefenseGroup,
    DraftableAsset,
    Player,
    RealTeam,
    Team,
    Universe,
    User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine  # noqa: E402

Base.metadata.create_all(bind=engine)
client = TestClient(app)

REAL_TEAMS = ["Georgia", "Notre Dame", "Oregon"]
SKILL_PLAYERS = [("QB", "Carson Beck"), ("RB", "Trevor Etienne"), ("WR", "Arian Smith")]
DEFENSE_GROUPS = ["DL", "LB", "DB"]


def seed_reference_data(db):
    ncaaf = Universe(sport="football", level="college", code="ncaaf", display_name="NCAAF")
    db.add(ncaaf)
    db.flush()

    assets_by_name = {}
    for index, team_name in enumerate(REAL_TEAMS):
        real_team = RealTeam(universe_id=ncaaf.id, name=team_name, classification="fbs")
        db.add(real_team)
        db.flush()

        for position, base_name in SKILL_PLAYERS:
            player = Player(
                universe_id=ncaaf.id,
                real_team_id=real_team.id,
                full_name=f"{base_name} ({team_name})",
                position=position,
            )
            db.add(player)
            db.flush()
            asset = DraftableAsset(universe_id=ncaaf.id, asset_type="player", player_id=player.id)
            db.add(asset)
            db.flush()
            assets_by_name[player.full_name] = asset.id

        # Each real team supplies three independent defensive lines - DL, LB
        # and DB are separate auction assets, never one bundled team defense
        # (docs/data_contract.md, defensive bidding granularity decision).
        for group_code in DEFENSE_GROUPS:
            group = DefenseGroup(real_team_id=real_team.id, position_group=group_code)
            db.add(group)
            db.flush()
            asset = DraftableAsset(
                universe_id=ncaaf.id, asset_type="defense_group", defense_group_id=group.id
            )
            db.add(asset)
            db.flush()
            assets_by_name[f"{team_name} {group_code}"] = asset.id

    commissioner = User(email="johnathan@example.com", display_name="Johnathan")
    db.add(commissioner)
    db.commit()
    return ncaaf, commissioner, assets_by_name


def main():
    db = SessionLocal()
    _, commissioner, assets_by_name = seed_reference_data(db)

    resp = client.post(
        "/leagues",
        json={
            "name": "Auction Smoke League",
            "commissioner_user_id": commissioner.id,
            "universe_codes": ["ncaaf"],
            "size": 3,
            "draft_rounds": 3,
        },
    )
    assert resp.status_code == 200, resp.text
    league = resp.json()
    print(f"Created league {league['id']}: {league['name']}")

    teams = [
        Team(league_id=league["id"], name="Alpha", owner_user_id=commissioner.id),
        Team(league_id=league["id"], name="Bravo", is_ai_owned=True),
        Team(league_id=league["id"], name="Charlie", is_ai_owned=True),
    ]
    db.add_all(teams)
    db.commit()
    alpha, bravo, charlie = [t.id for t in teams]
    names = {t.id: t.name for t in teams}

    # --- Open the Recruiting Period -------------------------------------
    resp = client.post(
        "/auctions/start",
        json={
            "league_id": league["id"],
            "slots": 3,
            "recruiting_period_seconds": 3 * 300,  # 5-minute rounds
            "team_tiers": {str(alpha): "large", str(bravo): "moderate", str(charlie): "small"},
        },
    )
    assert resp.status_code == 200, resp.text
    session = resp.json()
    auction_id = session["id"]
    print(
        f"Opened auction {auction_id}: {session['total_rounds']} per-slot rounds x "
        f"{session['round_seconds']}s = {session['recruiting_period_seconds']}s "
        f"(floor {session['minimum_round_seconds']}s)"
    )
    for budget in client.get(f"/auctions/{auction_id}/budgets").json():
        print(f"  {budget['team_name']:<8} {budget['tier_display_name']:<18} {budget['starting_budget']}")

    board = client.get(f"/auctions/{auction_id}/board").json()
    defense_lines = sorted(a["display_name"] for a in board if a["asset_type"] == "defense_group")
    print(f"\nRevealed pool: {len(board)} assets, of which {len(defense_lines)} defensive lines")
    print(f"  Georgia's lines on the board separately: "
          f"{[n for n in defense_lines if n.startswith('Georgia')]}")

    # --- Three rounds of bid -> reveal -> debit -> advance ----------------
    rounds = [
        # Round 1: Alpha and Bravo collide on the same QB; Charlie goes elsewhere.
        {
            alpha: ("Carson Beck (Georgia)", 90),
            bravo: ("Carson Beck (Georgia)", 75),
            charlie: ("Trevor Etienne (Oregon)", 40),
        },
        # Round 2: a dead-heat tie on one defensive line - "player choice" decides.
        {
            alpha: ("Georgia DL", 30),
            bravo: ("Georgia DL", 30),
            charlie: ("Georgia LB", 25),
        },
        # Round 3: three uncontested lines, including Georgia's remaining DB.
        {
            alpha: ("Georgia DB", 20),
            bravo: ("Notre Dame DL", 15),
            charlie: ("Arian Smith (Notre Dame)", 10),
        },
    ]

    for round_number, bids in enumerate(rounds, start=1):
        print(f"\n--- Round {round_number} ---")
        for team_id, (asset_name, amount) in bids.items():
            resp = client.post(
                f"/auctions/{auction_id}/bids",
                json={
                    "team_id": team_id,
                    "draftable_asset_id": assets_by_name[asset_name],
                    "amount": amount,
                },
            )
            assert resp.status_code == 200, resp.text
            print(f"  {names[team_id]} bids {amount} on {asset_name} (blind)")

        blind = client.get(f"/auctions/{auction_id}/rounds/{round_number}").json()
        assert blind["bids"] == [], "bids must stay hidden while the round is open"
        print(f"  Round is open: {blind['bid_count']} bids in, none visible yet")

        resp = client.post(
            f"/auctions/{auction_id}/rounds/{round_number}/resolve",
            params={"force": True, "seed": 2026},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        print("  Revealed:")
        for bid in body["bids"]:
            outcome = bid["status"] if bid["status"] == "won" else f"lost ({bid['lost_reason']})"
            print(f"    {names[bid['team_id']]:<8} {bid['amount']:>4} on {bid['display_name']:<28} {outcome}")
        for contest in body["contests"]:
            tag = " via random player choice" if contest["tie_break_used"] else ""
            print(
                f"  -> {contest['display_name']} to {names[contest['winning_team_id']]} "
                f"for {contest['winning_amount']}{tag}"
            )
        print(f"  Next round: {body['next_round_number']} (complete={body['session_complete']})")

    # --- Read the finished auction back out ------------------------------
    final = client.get(f"/auctions/{auction_id}").json()
    print(f"\nAuction status: {final['status']}")

    print("\nFinal budgets:")
    for budget in client.get(f"/auctions/{auction_id}/budgets").json():
        print(
            f"  {budget['team_name']:<8} spent {budget['spent_budget']:>4} of "
            f"{budget['starting_budget']:>4}, {budget['remaining_budget']:>4} left"
        )

    print("\nRosters built by the auction (read through the existing roster endpoint):")
    for team in teams:
        roster = client.get(f"/teams/{team.id}/roster").json()
        print(f"  {team.name}: {[entry['display_name'] for entry in roster]}")

    picks = client.get(f"/drafts/{session['draft_id']}/picks").json()
    print(f"\nThe auction's wins are ordinary draft picks too ({len(picks)} of them): "
          f"{[(p['round'], p['display_name']) for p in picks]}")

    print("\nAuction smoke test passed: a full Recruiting Period ran end to end.")


if __name__ == "__main__":
    main()
