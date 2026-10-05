"""
FS-006: one complete mock NFL Timed Auction Draft plus one scored NFL week, end to end through the real API.

  1. copies the prepared NFL database (nfl_score_run.db, read only - see scripts/run_nfl_scored_week.py for how it
     is built) to a throwaway file, so no existing *.db is modified
  2. creates a 12-team NFL league (draft_type "recruit", the Timed Auction Draft league type), every team AI-owned,
     4 Large / 4 Moderate / 4 Small
  3. starts the timed auction (POST /auctions/start) and drives it with POST /auctions/{id}/ai/tick through real
     round windows until the engine marks it complete; audits budgets, rosters, ties and nomination rotation
  4. sets every team's best lineup for the week, auto-schedules, then runs scripts/audit_scored_week.py on the result

Usage (repo root, API env):
    .venv-api/Scripts/python.exe scripts/run_nfl_mock_run.py --mode group --week 1 [--round-seconds 1]

Mock-only shortcuts (inherited from platform/ai/auction_ai.py and platform/lineups.py): the AI prices assets, and
best_lineup picks starters, from the database's own weekly scores. With only the audited week scored, that is
look-ahead, so the realism numbers are an upper bound on how well drafters do - not a drafting-quality claim.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
ap = argparse.ArgumentParser()
ap.add_argument("--source", default="nfl_score_run.db")
ap.add_argument("--db", default="nfl_mock_run.db")
ap.add_argument("--mode", choices=["group", "team", "individual"], default="group")
ap.add_argument("--week", type=int, default=1)
ap.add_argument("--teams", type=int, default=12)
ap.add_argument("--round-seconds", type=int, default=1)
args = ap.parse_args()
source, run_db = ROOT / args.source, ROOT / args.db
if not source.exists():
    sys.exit(f"{source} missing - see the preparation steps in scripts/run_nfl_scored_week.py")
shutil.copyfile(source, run_db)
os.environ["DATABASE_URL"] = f"sqlite:///{run_db.as_posix()}"

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.auction.constants import STARTING_BUDGET_BY_PROGRAM_SIZE
from fantasy_sports.platform.db.models import core  # noqa: F401
from fantasy_sports.platform.db.models.core import (
    AuctionBid, AuctionDraft, AuctionRound, AuctionTeamBudget, RosterEntry, Team, User,
)
from fantasy_sports.platform.db.session import SessionLocal


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    client = TestClient(app)
    db = SessionLocal()
    user = User(email=f"nfl-mock-{args.mode}@example.com", display_name="NFL Mock Commissioner")
    db.add(user)
    db.commit()
    H = {"Authorization": "Bearer " + client.post("/sessions", json={"user_id": user.id}).json()["token"]}

    r = client.post("/leagues", json={"name": f"NFL mock auction ({args.mode})", "commissioner_user_id": user.id,
                                      "universe_codes": ["nfl"], "size": args.teams, "draft_type": "recruit",
                                      "defense_modes": {"nfl": args.mode}}, headers=H)
    check(r.status_code == 200, r.text)
    league = r.json()
    lid, roster_size = league["id"], league["draft_rounds"]
    r = client.post("/teams", json={"league_id": lid, "name": "Mock Team 1", "program_size": "large",
                                    "is_ai_owned": True}, headers=H)
    check(r.status_code == 200, r.text)
    check(len(client.post(f"/leagues/{lid}/ai-teams", headers=H).json()) == args.teams - 1, "AI fill short")
    teams = client.get(f"/leagues/{lid}/teams").json()
    tiers = Counter(t["program_size"] for t in teams)
    check(len(teams) == args.teams and all(t["is_ai_owned"] for t in teams), "expected an all-AI league")
    print(f"League {lid} ({args.mode} defense, {args.teams} teams {dict(tiers)}, roster {roster_size}, draft_type {league['draft_type']})")

    aid = client.post("/auctions/start", json={"league_id": lid, "round_seconds": args.round_seconds}, headers=H).json()["id"]
    budgets = {b["team_id"]: b for b in client.get(f"/auctions/{aid}/budgets").json()}
    size_of = {t["id"]: t["program_size"] for t in teams}
    for tid, size in size_of.items():
        check(budgets[tid]["starting_budget"] == STARTING_BUDGET_BY_PROGRAM_SIZE[size], f"team {tid} budget off")

    t0, rounds_played = time.time(), 0
    while True:
        r = client.post(f"/auctions/{aid}/ai/tick")
        check(r.status_code == 200, r.text)
        if client.get(f"/auctions/{aid}").json()["status"] == "complete":
            break
        rnd = client.get(f"/auctions/{aid}/rounds/current").json()
        check(rounds_played < 1500, "auction exceeded 1500 rounds without completing")
        if rnd is None:
            continue
        rounds_played = rnd["round_number"]
        time.sleep(client.get(f"/auctions/{aid}/rounds/{rnd['id']}").json()["seconds_remaining"] + 0.05)
        if rounds_played % 50 == 0:
            print(f"  ...round {rounds_played}, {time.time() - t0:.0f}s elapsed")
    print(f"Auction complete after {rounds_played} rounds in {time.time() - t0:.0f}s")

    # ---- audit the auction against raw rows -------------------------------------------------------
    db.expire_all()
    a_row = db.get(AuctionDraft, aid)
    rounds = db.query(AuctionRound).filter_by(auction_draft_id=aid).order_by(AuctionRound.round_number).all()
    resolved = [x for x in rounds if x.status == "resolved"]
    spent, wins = Counter(), Counter()
    for x in resolved:
        spent[x.winning_team_id] += x.winning_bid_amount
        wins[x.winning_team_id] += 1
    entries = db.query(RosterEntry).filter(RosterEntry.team_id.in_(size_of), RosterEntry.acquired_via == "auction").all()
    check(len(entries) == len(resolved), "roster_entries != resolved rounds")
    check(len({e.draftable_asset_id for e in entries}) == len(entries), "an asset was drafted twice")
    ties = 0
    for x in resolved:
        bids = db.query(AuctionBid).filter_by(auction_round_id=x.id).all()
        top = max(b.amount for b in bids)
        tied = {b.team_id for b in bids if b.amount == top}
        check(x.winning_bid_amount == top and x.winning_team_id in tied, f"round {x.round_number}: bad winner")
        check(x.was_tie_broken_randomly == (len(tied) > 1), f"round {x.round_number}: tie flag wrong")
        ties += len(tied) > 1
    print(f"\n{'team':<16}{'tier':<10}{'start':>6}{'spent':>7}{'left':>6}{'won':>5}")
    partial = 0
    for tid in size_of:
        b = db.query(AuctionTeamBudget).filter_by(auction_draft_id=aid, team_id=tid).one()
        check(b.starting_budget - spent[tid] == b.remaining_budget >= 0, f"team {tid}: budget arithmetic off")
        check(wins[tid] == b.assets_won <= roster_size, f"team {tid}: wins/assets_won mismatch")
        partial += wins[tid] < roster_size
        print(f"{db.get(Team, tid).name:<16}{size_of[tid]:<10}{b.starting_budget:>6}{spent[tid]:>7}{b.remaining_budget:>6}{wins[tid]:>5}")
    check(a_row.status == "complete", "auction not marked complete")
    print(f"AUCTION PASSED: {len(resolved)} assets sold, {ties} tie-breaks verified, {partial}/{args.teams} rosters partial (allowed)")

    # ---- lineups, schedule, scored-week audit --------------------------------------------------------
    week = next((w for w in client.get(f"/leagues/{lid}/weeks").json() if w["week_number"] == args.week), None)
    check(week is not None, f"league has no week {args.week}")
    r = client.post(f"/leagues/{lid}/lineups/auto", params={"week_id": week["id"]}, headers=H)
    check(r.status_code == 200, r.text)
    r = client.post(f"/leagues/{lid}/schedule/auto", json={"weeks": 1}, headers=H)
    check(r.status_code == 200, r.text)
    print(f"Lineups set for week {args.week}; schedule created ({r.json()['matchups']} matchups)\n")
    sys.stdout.flush()
    sys.exit(subprocess.call([sys.executable, str(ROOT / "scripts" / "audit_scored_week.py"), args.db,
                              "--league", str(lid), "--week", str(args.week)]))


main()
