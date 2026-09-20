"""
Runs one complete mock CFB Timed Auction Draft end-to-end against the demo
league's dataset (the real 2024 CFBD rosters, defense groups, and weekly
scores that scripts/seed_demo_league.py seeds), driving the real HTTP API
with real timed round windows - no engine shortcuts, no simulated clock.

What it exercises and asserts, in order:
  league creation (recruit/auction draft type) -> 12 teams across the three
  program_size tiers (budgets 300/200/100, conferences auto-assigned) ->
  start auction -> per round: nominate in turn order, sealed bids from every
  team, bids hidden while open, early-resolve rejected, late-bid rejected,
  resolve after the window closes, reveal, tie-break, winner-only debit ->
  loop until the engine marks the auction complete -> final audit of budgets,
  rosters, tie-breaks, and nomination rotation (skipping full teams) against the raw DB rows.

Bots price assets off the demo dataset's own week 1-3 scores (a mock-only
shortcut - it uses "future" data, which is fine for validating the engine
but is not a real drafting strategy).

Usage (repo root):
    DATABASE_URL=sqlite:///./mock_draft_demo.db .venv/Scripts/python.exe scripts/seed_demo_league.py
    .venv-api/Scripts/python.exe scripts/run_mock_auction_draft.py [--round-seconds 1]

The seeded demo DB is only read for reference data; this script adds a new
league on top of it (the demo league's own snake-draft rosters are untouched).
"""

import argparse
import os
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("DATABASE_URL", "sqlite:///./mock_draft_demo.db")

from fastapi.testclient import TestClient

from fantasy_sports.api.helpers import draftable_asset_position
from fantasy_sports.api.main import app
from fantasy_sports.platform.auction.constants import MINIMUM_BID, STARTING_BUDGET_BY_PROGRAM_SIZE
from fantasy_sports.platform.db.session import SessionLocal
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
from fantasy_sports.sports.football.models import WeeklyPlayerScore

LEAGUE_SIZE = 12
ROSTER_SIZE = 20
PROGRAM_SIZES = ["large"] * 4 + ["moderate"] * 4 + ["small"] * 4
# Per-position roster caps a bot will fill (sums well past ROSTER_SIZE so a
# team can always find a legal buy) and the starter minimums it chases first.
POSITION_CAP = {"QB": 2, "RB": 5, "WR": 5, "TE": 3, "PK": 2, "DL": 2, "LB": 2, "DB": 2}
STARTER_MIN = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "PK": 1, "DL": 1, "LB": 1, "DB": 1}
MAX_ROUNDS = 600  # safety valve - a healthy run needs ~250

RNG = random.Random(2024)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--round-seconds", type=int, default=1)
    args = parser.parse_args()

    client = TestClient(app)
    db = SessionLocal()

    ncaaf = db.query(Universe).filter_by(code="ncaaf").first()
    check(ncaaf is not None, "no ncaaf universe - run scripts/seed_demo_league.py first")
    commissioner = db.query(User).first()

    # ---- value model: total demo-dataset points per asset ------------------
    value: dict[int, float] = defaultdict(float)
    for s in db.query(WeeklyPlayerScore):
        value[s.draftable_asset_id] += s.points
    assets = db.query(DraftableAsset).filter_by(universe_id=ncaaf.id).all()
    check(len(assets) >= LEAGUE_SIZE * ROSTER_SIZE, f"pool too small: {len(assets)}")
    position = {a.id: draftable_asset_position(a) for a in assets}
    print(f"Demo dataset: {len(assets)} draftable assets "
          f"({Counter(position.values()).most_common()}), {len(value)} with scored points")
    nonzero = [v for v in value.values() if v > 0]
    mean_value = sum(nonzero) / len(nonzero)

    # ---- league creation ---------------------------------------------------
    resp = client.post("/leagues", json={
        "name": "Mock Auction League", "commissioner_user_id": commissioner.id,
        "universe_codes": ["ncaaf"], "size": LEAGUE_SIZE, "draft_rounds": ROSTER_SIZE,
        "draft_type": "recruit",
    })
    check(resp.status_code == 200, resp.text)
    league = resp.json()
    print(f"\nCreated league {league['id']} ({league['name']}), draft_type=recruit")

    team_ids, size_of = [], {}
    for i, size in enumerate(PROGRAM_SIZES, start=1):
        body = {"league_id": league["id"], "name": f"Mock Team {i} ({size})", "program_size": size}
        if i == 1:
            body["owner_user_id"] = commissioner.id
        else:
            body["is_ai_owned"] = True
        resp = client.post("/teams", json=body)
        check(resp.status_code == 200, resp.text)
        team_ids.append(resp.json()["id"])
        size_of[resp.json()["id"]] = size
    confs = client.get(f"/leagues/{league['id']}/conferences").json()
    print(f"Teams {team_ids[0]}..{team_ids[-1]} created; conferences: "
          f"{[(c['name'], c['program_size'], c['team_count']) for c in confs]}")

    # ---- start auction -----------------------------------------------------
    resp = client.post("/auctions/start", json={"league_id": league["id"], "round_seconds": args.round_seconds})
    check(resp.status_code == 200, resp.text)
    auction = resp.json()
    aid = auction["id"]
    budgets = {b["team_id"]: b for b in client.get(f"/auctions/{aid}/budgets").json()}
    for tid in team_ids:
        check(budgets[tid]["starting_budget"] == STARTING_BUDGET_BY_PROGRAM_SIZE[size_of[tid]],
              f"team {tid} budget doesn't follow program_size")
    print(f"Auction {aid} started: budgets {sorted(set(b['starting_budget'] for b in budgets.values()), reverse=True)} "
          f"by tier, {args.round_seconds}s bid windows")

    # ---- bot state ---------------------------------------------------------
    won = {t: 0 for t in team_ids}
    remaining = {t: budgets[t]["starting_budget"] for t in team_ids}
    pos_count = {t: Counter() for t in team_ids}
    aggression = {t: RNG.uniform(0.7, 1.3) for t in team_ids}
    sold: set[int] = set()

    def wants(t, pos):
        if won[t] >= ROSTER_SIZE or pos not in POSITION_CAP:
            return False
        if pos_count[t][pos] < POSITION_CAP[pos]:
            return True
        # Past the per-position caps, a team with all starter minimums met will
        # take any position to fill out its bench - without this the endgame
        # stalls on whatever positions the pool has left (first run did).
        return not unmet_starters(t)

    def unmet_starters(t):
        return {p for p, n in STARTER_MIN.items() if pos_count[t][p] < n}

    def bid_amount(t, asset_id):
        slots_left = ROSTER_SIZE - won[t]
        ceiling = remaining[t] - (slots_left - 1) * MINIMUM_BID  # keep $1 for every other open slot
        if ceiling < MINIMUM_BID:
            return None
        fair = remaining[t] / slots_left
        raw = (value[asset_id] / mean_value) * fair * aggression[t]
        if position[asset_id] in unmet_starters(t):
            raw *= 1.25
        return max(MINIMUM_BID, min(ceiling, int(round(raw))))

    # ---- the draft loop ----------------------------------------------------
    log = []          # (round_number, nominator, asset, [(team, amount)], winner, amount, tied)
    early_resolve_checked = late_bid_checked = sealed_checked = False
    rounds_played = 0
    t0 = time.time()

    while True:
        state = client.get(f"/auctions/{aid}").json()
        if state["status"] == "complete":
            break
        recent = [x[4] for x in log[-LEAGUE_SIZE:]]
        check(not (len(recent) == LEAGUE_SIZE and all(w is None for w in recent)),
              "STALL: a full nomination cycle went unsold, but the engine still reports in_progress")
        check(rounds_played < MAX_ROUNDS, f"exceeded {MAX_ROUNDS} rounds without completing")

        nominator = state["nomination_order"][state["next_nominator_index"]]
        board = client.get(f"/auctions/{aid}/board").json()
        check(all(b["draftable_asset_id"] not in sold for b in board), "sold asset reappeared on the board")
        # Nominate the most valuable asset this team still needs (else the best
        # asset anyone might want) - full teams still have to nominate: the
        # engine rotates the turn regardless.
        need = unmet_starters(nominator)
        ranked = sorted(board, key=lambda b: value[b["draftable_asset_id"]], reverse=True)
        pick = next((b for b in ranked if b["position"] in need), None) \
            or next((b for b in ranked if wants(nominator, b["position"])), None) \
            or next((b for b in ranked if any(wants(t, b["position"]) for t in team_ids)), None) \
            or ranked[0]
        asset_id = pick["draftable_asset_id"]

        resp = client.post(f"/auctions/{aid}/nominate",
                           json={"draftable_asset_id": asset_id, "nominated_by_team_id": nominator})
        check(resp.status_code == 200, resp.text)
        rnd = resp.json()
        rounds_played += 1

        # window math: closes_at - opens_at must equal round_seconds
        from datetime import datetime
        span = (datetime.fromisoformat(rnd["closes_at"]) - datetime.fromisoformat(rnd["opens_at"])).total_seconds()
        check(span == args.round_seconds, f"round window is {span}s, expected {args.round_seconds}s")

        # a non-nominating team can't jump the nomination turn
        if rounds_played == 1:
            other = next(t for t in team_ids if t != nominator)
            r = client.post(f"/auctions/{aid}/nominate", json={"draftable_asset_id": ranked[-1]["draftable_asset_id"],
                                                               "nominated_by_team_id": other})
            check(r.status_code == 400, "nominating while a round is open should be rejected")

        placed = []
        for t in team_ids:
            if not wants(t, pick["position"]):
                continue
            amt = bid_amount(t, asset_id)
            if amt is None:
                continue
            r = client.post(f"/auctions/{aid}/rounds/{rnd['id']}/bids", json={"team_id": t, "amount": amt})
            check(r.status_code == 200, r.text)
            placed.append((t, amt))

        # over-budget bid must bounce
        if not sealed_checked and placed:
            t = placed[0][0]
            r = client.post(f"/auctions/{aid}/rounds/{rnd['id']}/bids",
                            json={"team_id": t, "amount": remaining[t] + 1})
            check(r.status_code == 400, "over-budget bid should be rejected")
            check(client.get(f"/auctions/{aid}/rounds/{rnd['id']}/bids").json() == [],
                  "bids must stay sealed while the round is open")
            sealed_checked = True

        # early resolve must be refused while the window is open
        if not early_resolve_checked:
            r = client.post(f"/auctions/{aid}/rounds/{rnd['id']}/resolve")
            check(r.status_code == 400 and "still open" in r.text, f"early resolve not refused: {r.text}")
            early_resolve_checked = True

        # wait for the real window to close
        wait = client.get(f"/auctions/{aid}/rounds/{rnd['id']}").json()["seconds_remaining"]
        time.sleep(wait + 0.05)

        # late bid must bounce
        if not late_bid_checked:
            r = client.post(f"/auctions/{aid}/rounds/{rnd['id']}/bids", json={"team_id": team_ids[0], "amount": 1})
            check(r.status_code == 400 and "closed" in r.text, f"late bid not refused: {r.text}")
            late_bid_checked = True

        resp = client.post(f"/auctions/{aid}/rounds/{rnd['id']}/resolve")
        check(resp.status_code == 200, resp.text)
        res = resp.json()

        if res["status"] == "resolved":
            revealed = client.get(f"/auctions/{aid}/rounds/{rnd['id']}/bids").json()
            check(sorted((b["team_id"], b["amount"]) for b in revealed) == sorted(placed),
                  "revealed bids don't match submitted bids")
            top = max(a for _, a in placed)
            tied = [t for t, a in placed if a == top]
            check(res["winning_bid_amount"] == top and res["winning_team_id"] in tied, "wrong winner")
            check(res["was_tie_broken_randomly"] == (len(tied) > 1), "tie flag wrong")
            w = res["winning_team_id"]
            remaining[w] -= top
            won[w] += 1
            pos_count[w][pick["position"]] += 1
            sold.add(asset_id)
            new_budgets = {b["team_id"]: b for b in client.get(f"/auctions/{aid}/budgets").json()}
            check(all(new_budgets[t]["remaining_budget"] == remaining[t] for t in team_ids),
                  "API budgets diverged from expected debits (a loser was debited or winner wasn't)")
            log.append((rnd["round_number"], nominator, rnd["display_name"], placed, w, top, len(tied)))
        else:
            check(res["status"] == "unsold" and not placed, f"unexpected status {res['status']}")
            log.append((rnd["round_number"], nominator, rnd["display_name"], placed, None, 0, 0))

        if rounds_played % 25 == 0:
            print(f"  ...{rounds_played} rounds, {len(sold)} sold, {time.time() - t0:.0f}s elapsed")

    print(f"\nAuction marked complete after {rounds_played} rounds ({len(sold)} sold, "
          f"{rounds_played - len(sold)} unsold) in {time.time() - t0:.0f}s")
    print("Live checks passed: window math, sealed bids, early-resolve refused, late-bid refused, "
          "over-budget refused, reveal matches, winner-only debit.")

    # ---- final audit against raw DB rows -----------------------------------
    db.expire_all()
    a_row = db.get(AuctionDraft, aid)
    rounds = db.query(AuctionRound).filter_by(auction_draft_id=aid).order_by(AuctionRound.round_number).all()
    order = a_row.nomination_order
    # Expected turn order: strict rotation, skipping any team whose roster is
    # already full as of that round.
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
    entries = db.query(RosterEntry).filter(RosterEntry.team_id.in_(team_ids), RosterEntry.acquired_via == "auction").all()
    check(len(entries) == len(resolved), "roster_entries != resolved rounds")
    check(len({e.draftable_asset_id for e in entries}) == len(entries), "an asset was drafted twice")

    ties = 0
    for r in resolved:
        bids = db.query(AuctionBid).filter_by(auction_round_id=r.id).all()
        top = max(b.amount for b in bids)
        tied = {b.team_id for b in bids if b.amount == top}
        check(r.winning_bid_amount == top and r.winning_team_id in tied, f"round {r.round_number}: bad winner")
        ties += len(tied) > 1

    print(f"\n{'team':<24}{'tier':<10}{'start':>6}{'spent':>7}{'left':>6}{'won':>5}   position mix")
    incomplete = 0
    for t in team_ids:
        team = db.get(Team, t)
        b = db.query(AuctionTeamBudget).filter_by(auction_draft_id=aid, team_id=t).one()
        check(b.starting_budget - spent[t] == b.remaining_budget >= 0, f"team {t}: budget arithmetic off")
        n = db.query(RosterEntry).filter_by(team_id=t, acquired_via="auction").count()
        check(n == b.assets_won, f"team {t}: roster rows != assets_won")
        incomplete += n < ROSTER_SIZE
        mix = " ".join(f"{p}{c}" for p, c in sorted(pos_count[t].items()))
        print(f"{team.name:<24}{team.program_size:<10}{b.starting_budget:>6}{spent[t]:>7}"
              f"{b.remaining_budget:>6}{n:>5}   {mix}")

    prices = sorted(((r.winning_bid_amount, r.draftable_asset.id) for r in resolved), reverse=True)
    print(f"\nTie-breaks: {ties} of {len(resolved)} resolved rounds went to a random tie-break "
          f"(all winners verified to be among the tied top bidders).")
    print(f"Highest prices: {[p for p, _ in prices[:5]]}; $1 buys: {sum(1 for p, _ in prices if p == 1)}")
    check(a_row.status == "complete", "auction not marked complete")
    print(f"\nMOCK DRAFT PASSED: {len(resolved)} assets sold to {LEAGUE_SIZE} teams; "
          f"{LEAGUE_SIZE - incomplete}/{LEAGUE_SIZE} rosters full at {ROSTER_SIZE}.")


if __name__ == "__main__":
    main()
