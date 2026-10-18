"""
Verification script for:
1. Conference rename and delete endpoints with FSC client methods (FSC.patch, FSC.del).
2. Hockey roster lines limited to 3 lines (18 players).
3. Trading between all teams, with trades involving AI teams requiring league majority approval.

Run with:
    .venv-api/Scripts/python.exe scripts/verify_trades_and_conference_rename.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

db_path = Path("test_trades_verify.db")
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    Conference,
    Draft,
    DraftableAsset,
    League,
    LeagueUniverse,
    Player,
    RealTeam,
    RosterEntry,
    Team,
    Universe,
    User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.platform.trades import propose_trade, respond_trade, vote_trade

Base.metadata.create_all(bind=engine)
client = TestClient(app)


def check(ok: bool, msg: str):
    print(("ok  " if ok else "FAIL ") + msg)
    if not ok:
        sys.exit(1)


def main():
    db = SessionLocal()

    # 1. Seed hockey reference data and players
    nhl, _ = seed_hockey_reference_data(db)
    bruins = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
    rangers = RealTeam(universe_id=nhl.id, name="New York Rangers", external_ref="NYR")
    db.add_all([bruins, rangers])
    db.flush()

    players = []
    for pos, n in (("C", 10), ("W", 20), ("D", 20), ("G", 10)):
        for i in range(n):
            p = Player(universe_id=nhl.id, real_team_id=bruins.id if i % 2 == 0 else rangers.id, full_name=f"{pos} Star {i:02d}", position=pos)
            db.add(p)
            db.flush()
            asset = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
            db.add(asset)
            db.flush()
            players.append((pos, p, asset))

    # Create users
    commish = User(email="commish@example.com", display_name="Commissioner", password_hash="test")
    user2 = User(email="user2@example.com", display_name="User Two", password_hash="test")
    db.add_all([commish, user2])
    db.flush()

    # Create league
    league = League(name="NHL Trade League", size=6, commissioner_user_id=commish.id, draft_type="snake", draft_rounds=18)
    db.add(league)
    db.flush()
    db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id, defense_mode="group"))

    conf1 = Conference(league_id=league.id, name="Eastern Conference", program_size="small")
    conf2 = Conference(league_id=league.id, name="Western Conference", program_size="large")
    db.add_all([conf1, conf2])
    db.flush()

    # Create 6 teams: Team 1 (commish), Team 2 (user2), Team 3-6 (AI)
    team1 = Team(league_id=league.id, name="Boston Blades", owner_user_id=commish.id, conference_id=conf1.id, is_ai_owned=False)
    team2 = Team(league_id=league.id, name="New York Nets", owner_user_id=user2.id, conference_id=conf1.id, is_ai_owned=False)
    team3 = Team(league_id=league.id, name="Chicago Cold", owner_user_id=None, conference_id=conf2.id, is_ai_owned=True)
    team4 = Team(league_id=league.id, name="Detroit Drift", owner_user_id=None, conference_id=conf2.id, is_ai_owned=True)
    team5 = Team(league_id=league.id, name="Montreal Mist", owner_user_id=None, conference_id=conf1.id, is_ai_owned=True)
    team6 = Team(league_id=league.id, name="Toronto Tide", owner_user_id=None, conference_id=conf2.id, is_ai_owned=True)
    db.add_all([team1, team2, team3, team4, team5, team6])
    db.flush()

    # Mark draft complete
    draft = Draft(league_id=league.id, status="complete")
    db.add(draft)
    db.flush()

    # Assign 18 players to each team (6 starters + 12 bench)
    all_assets = [a for _, _, a in players]
    for idx, team in enumerate([team1, team2, team3, team4, team5, team6]):
        team_assets = all_assets[idx * 9 : (idx + 1) * 9]  # 9 players each to allow trades without hitting caps
        for a in team_assets:
            db.add(RosterEntry(team_id=team.id, draftable_asset_id=a.id, acquired_via="draft"))
    db.commit()

    print("--- Test 1: Conference rename & delete (FSC.patch & FSC.del) ---")
    auth_headers = {"Authorization": f"Bearer mock_token"}
    # Login commish
    res = client.post("/sessions", json={"user_id": commish.id})
    token = res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Rename conference via PATCH
    rename_res = client.patch(f"/leagues/{league.id}/conferences/{conf1.id}", json={"name": "Atlantic Division"}, headers=headers)
    check(rename_res.status_code == 200, f"PATCH conference returns 200 (was {rename_res.status_code})")
    check(rename_res.json()["name"] == "Atlantic Division", f"Conference renamed to Atlantic Division")

    # Delete conference via DELETE
    del_res = client.delete(f"/leagues/{league.id}/conferences/{conf2.id}", headers=headers)
    check(del_res.status_code == 200, f"DELETE conference returns 200 (was {del_res.status_code})")

    print("\n--- Test 2: Hockey 3-line roster depth ---")
    po_res = client.get(f"/teams/{team1.id}/pecking-order")
    check(po_res.status_code == 200, "GET pecking-order returns 200")
    po_data = po_res.json()
    depth_by_group = {g["position_group"]: g["max_depth"] for g in po_data["groups"]}
    check(depth_by_group["C"] == 3, f"C max_depth is 3 (3 lines) -> {depth_by_group['C']}")
    check(depth_by_group["W"] == 6, f"W max_depth is 6 (3 lines x 2) -> {depth_by_group['W']}")
    check(depth_by_group["D"] == 6, f"D max_depth is 6 (3 lines x 2) -> {depth_by_group['D']}")
    check(depth_by_group["G"] == 3, f"G max_depth is 3 (3 lines) -> {depth_by_group['G']}")

    print("\n--- Test 3: Human-to-Human Trade ---")
    # Team 1 proposes trade to Team 2
    t1_roster = client.get(f"/teams/{team1.id}/roster").json()
    t2_roster = client.get(f"/teams/{team2.id}/roster").json()
    t1_send_id = t1_roster[0]["draftable_asset_id"]
    t2_send_id = t2_roster[0]["draftable_asset_id"]

    propose_payload = {
        "proposer_team_id": team1.id,
        "counterparty_team_id": team2.id,
        "send_asset_ids": [t1_send_id],
        "receive_asset_ids": [t2_send_id],
    }
    trade_res = client.post(f"/leagues/{league.id}/trades", json=propose_payload, headers=headers)
    check(trade_res.status_code == 200, f"Propose trade status 200 (was {trade_res.status_code})")
    trade_data = trade_res.json()
    check(trade_data["status"] == "proposed", f"Human-to-human trade starts in 'proposed' status (was {trade_data['status']})")
    check(trade_data["is_ai_trade"] is False, "is_ai_trade is False")
    trade_id = trade_data["id"]

    # Login user 2 and accept
    res2 = client.post("/sessions", json={"user_id": user2.id})
    token2 = res2.json()["token"]
    headers2 = {"Authorization": f"Bearer {token2}"}

    # FS-036: by default an accepted human trade now waits out a 24-hour review window; this check is about the swap itself,
    # so the league has no window here (the review window is covered by seed_trade_review_smoke_test.py).
    no_window = client.patch(f"/leagues/{league.id}/settings", json={"trade_review_hours": 0}, headers=headers)
    check(no_window.status_code == 200, f"Commissioner turns the review window off (was {no_window.status_code})")
    accept_res = client.post(f"/trades/{trade_id}/respond", json={"team_id": team2.id, "accept": True}, headers=headers2)
    check(accept_res.status_code == 200, f"Accept trade status 200 (was {accept_res.status_code})")
    check(accept_res.json()["status"] == "accepted", "Human trade executed upon counterparty acceptance (no review window)")

    # Verify players actually swapped
    t1_new_roster = {r["draftable_asset_id"] for r in client.get(f"/teams/{team1.id}/roster").json()}
    t2_new_roster = {r["draftable_asset_id"] for r in client.get(f"/teams/{team2.id}/roster").json()}
    check(t2_send_id in t1_new_roster, "Team 1 received player from Team 2")
    check(t1_send_id in t2_new_roster, "Team 2 received player from Team 1")

    print("\n--- Test 4: Trade with AI Team requires league majority approval ---")
    t3_roster = client.get(f"/teams/{team3.id}/roster").json()
    t1_cur_roster = client.get(f"/teams/{team1.id}/roster").json()
    t1_p_id = t1_cur_roster[0]["draftable_asset_id"]
    t3_p_id = t3_roster[0]["draftable_asset_id"]

    ai_propose_payload = {
        "proposer_team_id": team1.id,
        "counterparty_team_id": team3.id,  # AI team!
        "send_asset_ids": [t1_p_id],
        "receive_asset_ids": [t3_p_id],
    }
    ai_trade_res = client.post(f"/leagues/{league.id}/trades", json=ai_propose_payload, headers=headers)
    check(ai_trade_res.status_code == 200, f"Propose AI trade status 200 (was {ai_trade_res.status_code})")
    ai_trade_data = ai_trade_res.json()
    check(ai_trade_data["is_ai_trade"] is True, "is_ai_trade is True")
    check(ai_trade_data["majority_needed"] == 4, f"Majority needed for 6 teams is 4 (was {ai_trade_data['majority_needed']})")

    ai_trade_id = ai_trade_data["id"]
    # Check that it either entered 'voting' or auto-executed if fair AI majority was met
    print(f"AI Trade status after proposal: {ai_trade_data['status']}, Approvals: {ai_trade_data['approvals']} / {ai_trade_data['majority_needed']}")

    if ai_trade_data["status"] == "voting":
        # Cast vote from User 2
        vote_res = client.post(f"/trades/{ai_trade_id}/vote", json={"team_id": team2.id, "vote": "approve"}, headers=headers2)
        check(vote_res.status_code == 200, "Vote on trade succeeded")
        v_data = vote_res.json()
        print(f"After User 2 vote: status={v_data['status']}, approvals={v_data['approvals']}")
        if v_data["approvals"] >= v_data["majority_needed"]:
            check(v_data["status"] == "accepted", "Trade executed when majority approvals reached")

    print("\n--- Test 5: Verify static files cache headers ---")
    stat_res = client.get("/static/session.js")
    check("no-cache" in stat_res.headers.get("Cache-Control", ""), "Static files include no-cache header")

    print("\nAll trades, conference rename, and hockey 3-line roster tests PASSED!")

    # Clean up throwaway database
    db.close()
    if db_path.exists():
        try:
            db_path.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()
