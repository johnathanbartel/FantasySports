"""
Runs one complete mock CFB Timed Auction Draft (recruiting period)
end-to-end against the demo league's dataset (the real 2024 CFBD rosters,
defense groups, and weekly scores that scripts/seed_demo_league.py seeds),
driving the real HTTP API with real timed round windows - no engine
shortcuts, no simulated clock.

It plays the same flow a person does in the browser:
  sign in -> create league (recruit draft) -> create the human team (Large)
  -> "fill with AI teams" (12 teams, 4 Large / 4 Moderate / 4 Small, one
  conference per tier) -> start the recruiting period -> the human team
  nominates/bids through its session token while the 11 AI teams act
  server-side through POST /auctions/{id}/ai/tick -> loop until the engine
  marks the auction complete -> audit budgets, rosters, tie-breaks and
  nomination rotation against the raw DB rows.

Trust-gap checks (the API used to trust any team_id in the request body):
  no token -> 401; another user's token -> 403 on nominate/bid/team-create/
  ai-teams/start/force-resolve; the human token can't bid as an AI team;
  open-round bids are visible only to their owner.

The AI prices assets off the demo dataset's own weekly scores (a mock-only
shortcut - see platform/ai/auction_ai.py). Partial rosters are allowed: the
run ends when the engine's stall rule says so, not when rosters are full.

Usage (repo root):
    DATABASE_URL=sqlite:///./mock_draft_demo.db .venv/Scripts/python.exe scripts/seed_demo_league.py
    .venv-api/Scripts/python.exe scripts/run_mock_auction_draft.py [--round-seconds 1]

Runs against a throwaway copy of the seeded DB (mock_run.db); the seeded DB
itself is only read.
"""

import argparse
import os
import random
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SOURCE_DB, RUN_DB = ROOT / "mock_draft_demo.db", ROOT / "mock_run.db"
if not SOURCE_DB.exists():
    sys.exit("mock_draft_demo.db missing - run scripts/seed_demo_league.py first")
shutil.copyfile(SOURCE_DB, RUN_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{RUN_DB.as_posix()}"

from fastapi.testclient import TestClient

from fantasy_sports.api.helpers import draftable_asset_position
from fantasy_sports.api.main import app
from fantasy_sports.platform.ai.auction_ai import POSITION_CAP, STARTER_MIN, value_by_asset
from fantasy_sports.platform.auction.constants import MINIMUM_BID, STARTING_BUDGET_BY_PROGRAM_SIZE
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models import core
from fantasy_sports.platform.db.models.core import (
    AuctionBid,
    AuctionDraft,
    AuctionRound,
    AuctionTeamBudget,
    DraftableAsset,
    RosterEntry,
    Team,
    Universe,
    User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine

LEAGUE_SIZE = 12
ROSTER_SIZE = 20
MAX_ROUNDS = 700  # safety valve

RNG = random.Random(2024)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--round-seconds", type=int, default=1)
    args = parser.parse_args()

    Base.metadata.create_all(bind=engine)
    client = TestClient(app)
    db = SessionLocal()

    ncaaf = db.query(Universe).filter_by(code="ncaaf").first()
    check(ncaaf is not None, "no ncaaf universe - run scripts/seed_demo_league.py first")
    users = db.query(User).order_by(User.id).all()
    me_user, other_user = users[0], users[1]

    def login(user_id):
        r = client.post("/sessions", json={"user_id": user_id})
        check(r.status_code == 200, r.text)
        return {"Authorization": "Bearer " + r.json()["token"]}

    me, other = login(me_user.id), login(other_user.id)

    # ---- value model (same mock-only shortcut the AI uses) ------------------
    value = value_by_asset(db)
    assets = db.query(DraftableAsset).filter_by(universe_id=ncaaf.id).all()
    position = {a.id: draftable_asset_position(a) for a in assets}
    nonzero = [v for v in value.values() if v > 0]
    mean_value = sum(nonzero) / len(nonzero)
    print(f"Demo dataset: {len(assets)} draftable assets, {len(nonzero)} with non-zero points")

    # ---- league + teams -------------------------------------------------------
    league_body = {"name": "Mock Recruiting League", "commissioner_user_id": me_user.id,
                   "universe_codes": ["ncaaf"], "size": LEAGUE_SIZE, "draft_rounds": ROSTER_SIZE,
                   "draft_type": "recruit"}
    check(client.post("/leagues", json=league_body).status_code == 401, "league creation without a token must be 401")
    check(client.post("/leagues", json=league_body, headers=other).status_code == 403,
          "creating a league as someone else must be 403")
    league = client.post("/leagues", json=league_body, headers=me).json()
    lid = league["id"]
    print(f"Created league {lid}, draft_type={league['draft_type']}")

    body = {"league_id": lid, "name": "Mock Human (large)", "program_size": "large", "owner_user_id": me_user.id}
    check(client.post("/teams", json=body, headers=other).status_code == 403, "creating a team for someone else must be 403")
    my_team = client.post("/teams", json=body, headers=me).json()["id"]

    check(client.post(f"/leagues/{lid}/ai-teams", headers=other).status_code == 403, "non-commissioner AI fill must be 403")
    ai_teams = client.post(f"/leagues/{lid}/ai-teams", headers=me).json()
    teams = client.get(f"/leagues/{lid}/teams").json()
    tiers = Counter(t["program_size"] for t in teams)
    check(len(teams) == LEAGUE_SIZE and len(ai_teams) == LEAGUE_SIZE - 1, f"expected 12 teams, got {len(teams)}")
    check(tiers == {"large": 4, "moderate": 4, "small": 4}, f"tiers not 4/4/4: {tiers}")
    confs = client.get(f"/leagues/{lid}/conferences").json()
    check(all(c["team_count"] == 4 for c in confs), f"conference sizes off: {confs}")
    check(client.post("/teams", json={"league_id": lid, "name": "Overflow", "program_size": "small",
                                      "is_ai_owned": True}, headers=me).status_code == 400, "league should be full")
    check(client.post(f"/leagues/{lid}/ai-teams", headers=me).json() == [], "second AI fill should add nothing")
    size_of = {t["id"]: t["program_size"] for t in teams}
    ai_ids = [t["id"] for t in teams if t["is_ai_owned"]]
    print(f"Teams: {dict(tiers)}; conferences: {[(c['name'], c['team_count']) for c in confs]}")

    # ---- start auction ------------------------------------------------------
    sr = {"league_id": lid, "round_seconds": args.round_seconds}
    check(client.post("/auctions/start", json=sr).status_code == 401, "start without token must be 401")
    check(client.post("/auctions/start", json=sr, headers=other).status_code == 403, "non-commissioner start must be 403")
    aid = client.post("/auctions/start", json=sr, headers=me).json()["id"]
    budgets = {b["team_id"]: b for b in client.get(f"/auctions/{aid}/budgets").json()}
    for tid, size in size_of.items():
        check(budgets[tid]["starting_budget"] == STARTING_BUDGET_BY_PROGRAM_SIZE[size], f"team {tid} budget off")
    print(f"Auction {aid} started: budgets by tier {sorted({b['starting_budget'] for b in budgets.values()}, reverse=True)}, "
          f"{args.round_seconds}s windows")

    def tick():
        r = client.post(f"/auctions/{aid}/ai/tick")
        check(r.status_code == 200, r.text)
        return r.json()["actions"]

    # ---- human bot state ------------------------------------------------------
    pos_count = Counter()
    won = [0]
    remaining = [budgets[my_team]["starting_budget"]]

    def unmet():
        return {p for p, n in STARTER_MIN.items() if pos_count[p] < n}

    def wants(pos):
        if won[0] >= ROSTER_SIZE or pos not in POSITION_CAP:
            return False
        return pos_count[pos] < POSITION_CAP[pos] or not unmet()

    def my_bid(asset_id):
        slots_left = ROSTER_SIZE - won[0]
        ceiling = remaining[0] - (slots_left - 1) * MINIMUM_BID
        if ceiling < MINIMUM_BID:
            return None
        raw = (value.get(asset_id, 0) / mean_value) * (remaining[0] / slots_left) * RNG.uniform(0.7, 1.3)
        if position[asset_id] in unmet():
            raw *= 1.25
        return max(MINIMUM_BID, min(ceiling, int(round(raw))))

    # ---- the draft loop ------------------------------------------------------
    rounds_played, checks = 0, set()
    human_rounds_nominated = 0
    t0 = time.time()
    bid_on = None  # round id the human bot has already bid in

    while True:
        tick()
        state = client.get(f"/auctions/{aid}").json()
        if state["status"] == "complete":
            break
        rnd = client.get(f"/auctions/{aid}/rounds/current").json()
        check(rounds_played < MAX_ROUNDS, f"exceeded {MAX_ROUNDS} rounds without completing")

        if rnd is None:
            nominator = state["nomination_order"][state["next_nominator_index"]]
            check(nominator == my_team, f"AI team {nominator} is on the clock but the tick didn't nominate")
            board = client.get(f"/auctions/{aid}/board").json()
            ranked = sorted(board, key=lambda b: value.get(b["draftable_asset_id"], 0), reverse=True)
            pick = next((b for b in ranked if b["position"] in unmet()), None) \
                or next((b for b in ranked if wants(b["position"])), None) or ranked[0]

            # Session checks, once, while it's my turn: no token / wrong owner.
            if "nominate" not in checks:
                payload = {"draftable_asset_id": pick["draftable_asset_id"], "nominated_by_team_id": my_team}
                check(client.post(f"/auctions/{aid}/nominate", json=payload).status_code == 401, "nominate w/o token must be 401")
                check(client.post(f"/auctions/{aid}/nominate", json=payload, headers=other).status_code == 403,
                      "nominating as another user's team must be 403")
                ai_payload = {"draftable_asset_id": pick["draftable_asset_id"], "nominated_by_team_id": ai_ids[0]}
                check(client.post(f"/auctions/{aid}/nominate", json=ai_payload, headers=me).status_code == 403,
                      "my token nominating as an AI team must be 403")
                checks.add("nominate")

            r = client.post(f"/auctions/{aid}/nominate", headers=me, json={
                "draftable_asset_id": pick["draftable_asset_id"], "nominated_by_team_id": my_team})
            check(r.status_code == 200, r.text)
            rnd = r.json()
            human_rounds_nominated += 1
            tick()  # AI teams place their sealed bids on it

        rounds_played = rnd["round_number"]
        span = (datetime.fromisoformat(rnd["closes_at"]) - datetime.fromisoformat(rnd["opens_at"])).total_seconds()
        check(span == args.round_seconds, f"round window is {span}s, expected {args.round_seconds}s")
        asset_id, pos = rnd["draftable_asset_id"], position[rnd["draftable_asset_id"]]

        # human bid (once per round)
        my_amount = None
        if bid_on != rnd["id"] and wants(pos):
            my_amount = my_bid(asset_id)
            if my_amount is not None:
                path = f"/auctions/{aid}/rounds/{rnd['id']}/bids"
                if "bid" not in checks:
                    check(client.post(path, json={"team_id": my_team, "amount": my_amount}).status_code == 401, "bid w/o token must be 401")
                    check(client.post(path, json={"team_id": my_team, "amount": my_amount}, headers=other).status_code == 403,
                          "bidding as another user's team must be 403")
                    check(client.post(path, json={"team_id": ai_ids[0], "amount": 1}, headers=me).status_code == 403,
                          "human token bidding as an AI team must be 403")
                    check(client.post(path, json={"team_id": my_team, "amount": remaining[0] + 1}, headers=me).status_code == 400,
                          "over-budget bid should be rejected")
                    checks.add("bid")
                r = client.post(path, json={"team_id": my_team, "amount": my_amount}, headers=me)
                check(r.status_code == 200, r.text)
                bid_on = rnd["id"]

        # sealed: open-round bids reveal nothing to anyone else; owner sees only own
        if "sealed" not in checks:
            path = f"/auctions/{aid}/rounds/{rnd['id']}/bids"
            check(client.get(path).json() == [], "anonymous caller must see no open-round bids")
            mine = client.get(path, headers=me).json()
            check([b["team_id"] for b in mine] == ([my_team] if my_amount else []), "owner should see only their own sealed bid")
            check(client.get(path, headers=other).json() == [], "another user must see no open-round bids")
            checks.add("sealed")

        if "early" not in checks:
            r = client.post(f"/auctions/{aid}/rounds/{rnd['id']}/resolve")
            check(r.status_code == 400 and "still open" in r.text, f"early resolve not refused: {r.text}")
            check(client.post(f"/auctions/{aid}/rounds/{rnd['id']}/resolve?force=true").status_code == 401, "force w/o token must be 401")
            check(client.post(f"/auctions/{aid}/rounds/{rnd['id']}/resolve?force=true", headers=other).status_code == 403,
                  "force-resolve by a non-commissioner must be 403")
            checks.add("early")

        wait = client.get(f"/auctions/{aid}/rounds/{rnd['id']}").json()["seconds_remaining"]
        time.sleep(wait + 0.05)

        if "late" not in checks and my_amount is not None:
            r = client.post(f"/auctions/{aid}/rounds/{rnd['id']}/bids", json={"team_id": my_team, "amount": 1}, headers=me)
            check(r.status_code == 400 and "closed" in r.text, f"late bid not refused: {r.text}")
            checks.add("late")

        tick()  # resolves the expired round (and any AI nomination that follows)
        done = client.get(f"/auctions/{aid}/rounds/{rnd['id']}").json()
        check(done["status"] in ("resolved", "unsold"), f"round {rnd['id']} not resolved by the tick: {done['status']}")
        if done["status"] == "resolved" and done["winning_team_id"] == my_team:
            won[0] += 1
            remaining[0] -= done["winning_bid_amount"]
            pos_count[pos] += 1

        if rounds_played % 25 == 0:
            print(f"  ...round {rounds_played}, {time.time() - t0:.0f}s elapsed")

    print(f"\nAuction marked complete after {rounds_played} rounds in {time.time() - t0:.0f}s "
          f"({human_rounds_nominated} nominated by the human team, the rest by AI).")
    print(f"Live checks passed: {sorted(checks)} + 401/403 on every gated action.")

    # ---- final audit against raw DB rows ------------------------------------
    db.expire_all()
    a_row = db.get(AuctionDraft, aid)
    rounds = db.query(AuctionRound).filter_by(auction_draft_id=aid).order_by(AuctionRound.round_number).all()
    order = a_row.nomination_order

    idx, wins = 0, Counter()
    for r in rounds:
        check(r.nominated_by_team_id == order[idx], f"round {r.round_number}: nomination rotation broken")
        if r.status == "resolved":
            wins[r.winning_team_id] += 1
        for step in range(1, len(order) + 1):
            nxt = (idx + step) % len(order)
            if wins[order[nxt]] < ROSTER_SIZE:
                idx = nxt
                break
    print(f"Nomination rotation held across all {len(rounds)} rounds (full teams skipped).")

    resolved = [r for r in rounds if r.status == "resolved"]
    spent = Counter()
    for r in resolved:
        spent[r.winning_team_id] += r.winning_bid_amount
    entries = db.query(RosterEntry).filter(RosterEntry.team_id.in_(size_of), RosterEntry.acquired_via == "auction").all()
    check(len(entries) == len(resolved), "roster_entries != resolved rounds")
    check(len({e.draftable_asset_id for e in entries}) == len(entries), "an asset was drafted twice")

    ties = 0
    for r in resolved:
        bids = db.query(AuctionBid).filter_by(auction_round_id=r.id).all()
        top = max(b.amount for b in bids)
        tied = {b.team_id for b in bids if b.amount == top}
        check(r.winning_bid_amount == top and r.winning_team_id in tied, f"round {r.round_number}: bad winner")
        check(r.was_tie_broken_randomly == (len(tied) > 1), f"round {r.round_number}: tie flag wrong")
        ties += len(tied) > 1

    print(f"\n{'team':<24}{'tier':<10}{'start':>6}{'spent':>7}{'left':>6}{'won':>5}   AI?")
    partial = 0
    for t in size_of:
        team = db.get(Team, t)
        b = db.query(AuctionTeamBudget).filter_by(auction_draft_id=aid, team_id=t).one()
        check(b.starting_budget - spent[t] == b.remaining_budget >= 0, f"team {t}: budget arithmetic off")
        n = db.query(RosterEntry).filter_by(team_id=t, acquired_via="auction").count()
        check(n == b.assets_won, f"team {t}: roster rows != assets_won")
        partial += n < ROSTER_SIZE
        print(f"{team.name:<24}{team.program_size:<10}{b.starting_budget:>6}{spent[t]:>7}"
              f"{b.remaining_budget:>6}{n:>5}   {'AI' if team.is_ai_owned else 'human'}")

    print(f"\nTie-breaks: {ties} of {len(resolved)} resolved rounds (all winners verified among the tied top bidders).")
    check(a_row.status == "complete", "auction not marked complete")
    print(f"MOCK DRAFT PASSED: {len(resolved)} assets sold to {LEAGUE_SIZE} teams; "
          f"{partial}/{LEAGUE_SIZE} rosters ended partial (allowed).")


if __name__ == "__main__":
    main()
