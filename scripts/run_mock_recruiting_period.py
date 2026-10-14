"""
Runs one complete mock recruiting draft end-to-end against the demo league's
dataset, driving the real HTTP API (sessions, ownership checks, the
server-side AI tick) - the same flow a person follows in the browser.

The recruiting draft is a series of fixed-length periods - WEEKS to the user -
one per roster spot (20 for a 20-man roster), each as long as the commissioner
sets. The program-size budget ($300 / $200 / $100) is a WEEKLY budget that
accumulates, and a player's hidden price is his cost for the whole recruitment
(avg $10 x 20 weeks = $200; elite avg $300). Between weeks there is a buffer
(20 seconds in the product, 1 second here) with no bidding, and each team gets an
end-of-week results screen: per bid, won / lost / carried over, and the player's
completion % (total bids / hidden price) - visible nowhere else. No team can overspend: a bid is
rejected if it would take the team past the money accrued so far. It is not a one-player-at-a-time auction:
once it opens, any team can bid on any player at any time. Every player has a
hidden price. Bids are processed only when a period ENDS: a player whose bids
sum to his price goes to the highest bidder, and EVERY team that bid on him is
charged its own bid, win or lose; the rest carry over, uncharged. When the final period ends, the highest bidder gets every
remaining player who has bids, regardless of price.

Checks, in order:
  league creation with a scheduled start and a commissioner-set period length
  -> "scheduled" until the time passes (lazy open) -> 12 teams, 4/4/4 ->
  hidden prices: the drafted pool averages ~$200 and its top 20% ~$300, cheap below it, unbiased random draws -> no endpoint leaks a price,
  other teams' bid amounts stay hidden, the pool carries last season's
  ESPN-standard points -> weekly budgets accumulate week to week -> a bid that
  would overspend is rejected (single bid, and the sum of standing bids) ->
  401/403 gating -> bids are NOT processed mid-period, and the real clock
  processes period 1 on its own -> highest bidder wins, charged their own bid
  -> a short-of-price bid carries over and is awarded once later bids
  complete it -> free-for-all bidding period by period (AI teams via
  /ai/tick, the human team via its token) -> the final period settles unmet
  players at the deadline -> audit every award against the raw bids.

The AI and the elite/price model use the demo dataset's own weekly scores (a
mock-only shortcut: they see the future).

Usage (repo root):
    DATABASE_URL=sqlite:///./mock_draft_demo.db .venv/Scripts/python.exe scripts/seed_demo_league.py
    .venv-api/Scripts/python.exe scripts/run_mock_recruiting_period.py

Runs on a throwaway copy of the seeded DB (mock_run.db).
"""

import datetime
import os
import random
import shutil
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SOURCE_DB, RUN_DB = ROOT / "mock_draft_demo.db", ROOT / "mock_run.db"
if not SOURCE_DB.exists():
    sys.exit("mock_draft_demo.db missing - run scripts/seed_demo_league.py first")
shutil.copyfile(SOURCE_DB, RUN_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{RUN_DB.as_posix()}"

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.auction.constants import STARTING_BUDGET_BY_PROGRAM_SIZE
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models import core  # noqa: F401
from fantasy_sports.platform.db.models.core import (
    RecruitingAward,
    RecruitingBid,
    RecruitingBidResult,
    RecruitingCharge,
    RecruitingDraft,
    DraftableAsset,
    RecruitingThreshold,
    RosterEntry,
    User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine as db_engine
from fantasy_sports.platform.recruiting import engine as recruiting
from fantasy_sports.platform.recruiting.pricing import expected_prices, generate_thresholds, price_inputs, value_by_asset

LEAGUE_SIZE, ROSTER_SIZE = 12, 20
PERIOD_SECONDS = 10
BUFFER_SECONDS = 1   # 20 in the product; shortened so the script doesn't wait it out 20 times
TICKS_PER_PERIOD = 8


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    from fantasy_sports.platform.db.schema_sync import ensure_latest_schema
    ensure_latest_schema(db_engine)
    client = TestClient(app)
    db = SessionLocal()
    rng = random.Random(7)

    users = db.query(User).order_by(User.id).all()
    me_user, other_user = users[0], users[1]

    def login(uid):
        r = client.post("/sessions", json={"user_id": uid})
        check(r.status_code == 200, r.text)
        return {"Authorization": "Bearer " + r.json()["token"]}

    me, other = login(me_user.id), login(other_user.id)

    # ---- league with a scheduled start and a period length ------------------
    start_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=3)
    league = client.post("/leagues", headers=me, json={
        "name": "Mock Recruiting League", "commissioner_user_id": me_user.id, "universe_codes": ["ncaaf"],
        "size": LEAGUE_SIZE, "draft_rounds": ROSTER_SIZE, "draft_type": "recruit",
        "draft_scheduled_at": start_at.isoformat() + "Z", "recruiting_period_seconds": PERIOD_SECONDS,
        "recruiting_buffer_seconds": BUFFER_SECONDS}).json()
    lid = league["id"]
    my_team = client.post("/teams", headers=me, json={
        "league_id": lid, "name": "Mock Human (large)", "program_size": "large", "owner_user_id": me_user.id}).json()["id"]
    client.post(f"/leagues/{lid}/ai-teams", headers=me)
    teams = client.get(f"/leagues/{lid}/teams").json()
    check(Counter(t["program_size"] for t in teams) == {"large": 4, "moderate": 4, "small": 4}, "tiers not 4/4/4")
    ai_ids = [t["id"] for t in teams if t["is_ai_owned"]]
    print(f"League {lid}: 12 teams, 4/4/4, {ROSTER_SIZE} periods of {PERIOD_SECONDS}s, starts in 3s")

    st = client.get(f"/recruiting/league/{lid}").json()
    check(st["status"] == "scheduled" and st["draft_id"] is None, f"should be scheduled, got {st}")
    check(st["total_periods"] == ROSTER_SIZE and st["period_seconds"] == PERIOD_SECONDS, f"period settings wrong: {st}")
    check(client.post("/recruiting/start", json={"league_id": lid}, headers=other).status_code == 403,
          "start-now by a non-commissioner must be 403")
    time.sleep(3.2)
    st = client.get(f"/recruiting/league/{lid}").json()
    check(st["status"] == "open" and st["current_period"] == 1, f"draft should have opened at its scheduled time: {st}")
    did = st["draft_id"]
    check(client.post("/recruiting/start", json={"league_id": lid}, headers=me).status_code == 400,
          "starting an already-open draft must be rejected")
    print(f"Draft {did} opened itself at the scheduled time; period 1 of {st['total_periods']}")

    # ---- hidden prices --------------------------------------------------------
    thr = db.query(RecruitingThreshold).filter_by(draft_id=did).all()
    price = {t.draftable_asset_id: t.threshold for t in thr}
    value, drafted, groups = price_inputs(db, list(price), LEAGUE_SIZE, ROSTER_SIZE)   # worth = value over replacement
    elite_ids = [t.draftable_asset_id for t in thr if t.is_elite]
    d_mean = sum(price[a] for a in drafted) / len(drafted)
    e_mean = sum(price[a] for a in elite_ids) / len(elite_ids)
    check(abs(d_mean - 10 * ROSTER_SIZE) <= 25, f"drafted-pool mean price {d_mean:.0f} not ~{10 * ROSTER_SIZE}")
    check(abs(e_mean - 15 * ROSTER_SIZE) <= 60, f"elite mean price {e_mean:.0f} not ~{15 * ROSTER_SIZE}")
    check(len(elite_ids) == round(0.2 * len(drafted)), f"elite tier should be the top 20% of the drafted pool: {len(elite_ids)}")
    # Below the drafted pool prices fall away: the median undrafted player costs far less than a drafted one.
    drafted_set = set(drafted)
    rest = sorted(price[a] for a in price if a not in groups and a not in drafted_set)
    check(rest[len(rest) // 2] < 0.4 * d_mean, f"undrafted players should be cheap: median ${rest[len(rest) // 2]} vs ${d_mean:.0f}")
    # Every league draws its own prices around an expected price: over many leagues the average is the expectation.
    expected, _ = expected_prices(list(price), value, ROSTER_SIZE, drafted, groups)
    draws = [generate_thresholds(list(price), value, random.Random(s), ROSTER_SIZE, drafted, groups) for s in range(150)]
    ratio = sum(sum(d[a][0] for d in draws) / len(draws) for a in drafted) / sum(expected[a] for a in drafted)
    check(abs(ratio - 1) <= 0.02, f"prices are biased: mean price / expected = {ratio:.3f}")
    check(sum(1 for d in draws[:40] for a in drafted if a in set(elite_ids) and d[a][0] <= 0.4 * expected[a]) >= 5, "cheap elites missing")
    check(sum(1 for d in draws[:40] for a in drafted if d[a][0] >= 2 * expected[a]) >= 20, "expensive role players missing")
    check(len({price[a] for a in drafted}) > 50, "prices aren't randomised")
    print(f"Hidden prices (whole-recruitment cost): drafted pool avg ${d_mean:.0f}, {len(elite_ids)} elite avg ${e_mean:.0f}; "
          f"unbiased over 150 leagues (mean/expected {ratio:.3f})")

    pool = client.get(f"/recruiting/{did}/pool").json()
    check(len(pool) == len(thr), "pool size != priced players")
    check("threshold" not in client.get(f"/recruiting/{did}/state", headers=me).text.lower()
          and "threshold" not in str(pool[:5]).lower(), "a price leaked")

    # last season's points ride along on the pool, so the board can sort by them
    with_pts = [p for p in pool if p["last_season_points"] is not None]
    check(len(with_pts) > 300, f"only {len(with_pts)} players carry 2024 points - run scripts/ingest_season_points.py")
    top = max(with_pts, key=lambda p: p["last_season_points"])
    check(top["position"] == "QB" and top["last_season_points"] > 300, f"unexpected top scorer {top}")
    check(all(p["last_season_points"] is None for p in pool if p["position"] in ("DL", "LB", "DB")), "defense groups must have no points")
    check(all(p["real_team"] for p in pool), "pool rows should carry the real team")
    print(f"Pool: {len(with_pts)} players carry 2024 ESPN-standard points (top: {top['display_name']} {top['last_season_points']}); "
          f"defense groups carry none")

    def bid(team_id, asset_id, amount, headers=me):
        return client.put(f"/recruiting/{did}/bids", headers=headers,
                          json={"team_id": team_id, "draftable_asset_id": asset_id, "amount": amount})

    def state():
        return client.get(f"/recruiting/{did}/state", headers=me).json()

    def process_now(headers=me):
        return client.post(f"/recruiting/{did}/process-period", headers=headers)

    def wait_buffer():
        """Sit out the pause between weeks (no bids can be placed during it)."""
        st_ = client.get(f"/recruiting/league/{lid}").json()
        if st_["phase"] == "buffer":
            wait_ = (datetime.datetime.fromisoformat(st_["next_period_starts_at"]) - datetime.datetime.utcnow()).total_seconds()
            time.sleep(max(0, wait_) + 0.05)

    RP = f"/recruiting/{did}/results/pending"

    # ---- gating -------------------------------------------------------------
    any_asset = pool[0]["draftable_asset_id"]
    check(client.put(f"/recruiting/{did}/bids", json={"team_id": my_team, "draftable_asset_id": any_asset, "amount": 1}).status_code == 401,
          "bid w/o token must be 401")
    check(bid(my_team, any_asset, 1, other).status_code == 403, "bid as someone else must be 403")
    check(bid(ai_ids[0], any_asset, 1).status_code == 403, "human token bidding as an AI team must be 403")
    check(process_now(other).status_code == 403, "process-period by a non-commissioner must be 403")
    check(client.post(f"/recruiting/{did}/end", headers=other).status_code == 403, "end by a non-commissioner must be 403")

    # ---- no overspending: week 1 gives the Large human team exactly $300 --------------
    D, E = [a for a in price if a not in (any_asset,)][:2]
    over = bid(my_team, D, 301)
    check(over.status_code == 400 and "afford" in over.text, f"a bid above the available money must be rejected: {over.text}")
    check(bid(my_team, D, 250).status_code == 200, "a bid within the available money must be accepted")
    over2 = bid(my_team, E, 51)     # 250 + 51 > 300: the SUM of standing bids can't exceed the money either
    check(over2.status_code == 400 and "afford" in over2.text, f"a second bid past the total must be rejected: {over2.text}")
    check(bid(my_team, E, 50).status_code == 200, "a bid that exactly uses up the money must be accepted")
    mine_f = next(t for t in state()["teams"] if t["team_id"] == my_team)
    check(mine_f["committed"] == 300 and mine_f["spendable"] == 0 and mine_f["weekly_budget"] == 300, f"my finance wrong: {mine_f}")
    check(bid(my_team, D, 100).status_code == 200, "lowering a bid must always be allowed")
    check(bid(my_team, D, 250).status_code == 200, "raising back into freed room must be allowed")
    check(all(t["committed"] is None and t["spendable"] is None for t in state()["teams"] if t["team_id"] != my_team),
          "other teams' commitments must stay private")
    bid(my_team, D, 0); bid(my_team, E, 0)
    check(next(t for t in state()["teams"] if t["team_id"] == my_team)["committed"] == 0, "retracting should free the money")
    print("No overspending: a $301 bid and a second bid past the $300 total were both rejected; lowering/retracting frees the room")

    # ---- period 1: bids are NOT processed mid-period; the real clock processes them ----
    large_ai = [t["id"] for t in teams if t["is_ai_owned"] and t["program_size"] == "large"]
    A = next(a for a in price if 100 <= price[a] <= 290)
    T = price[A]
    x = large_ai[0]                                   # a Large team can afford T-3 (<= $287) in week 1
    y = next(i for i in ai_ids if i != x)
    recruiting.place_bid(db, did, x, A, T - 3)   # highest bidder, alone short of the price
    recruiting.place_bid(db, did, y, A, 2)        # sum T-1: still short
    db.commit()
    check(bid(my_team, A, 1).status_code == 200, "bid rejected")  # sum == T: price met, but the period isn't over
    F = next(a for a in price if a != A and price[a] >= 100 and a not in (D, E))
    check(bid(my_team, F, 10).status_code == 200, "bid rejected")  # far short of F's price: carries over
    check(all(a["draftable_asset_id"] != A for a in state()["awards"]), "awarded mid-period - bids must wait for the period to end")
    check(client.get(f"/recruiting/league/{lid}").json()["current_period"] == 1, "still period 1")

    # own-bid visibility and retraction
    B = next(a for a in price if a not in (A, F) and price[a] >= 100)
    check(bid(my_team, B, 3).status_code == 200, "bid rejected")
    check([(b["draftable_asset_id"], b["amount"]) for b in state()["my_bids"] if b["draftable_asset_id"] == B] == [(B, 3)],
          "own bid not visible / wrong")
    check(client.get(f"/recruiting/{did}/state", headers=other).json()["my_bids"] == [], "another user must not see my bids")
    bid(my_team, B, 0)
    check(all(b["draftable_asset_id"] != B for b in state()["my_bids"]), "retract failed")

    wait = (datetime.datetime.fromisoformat(state()["period_ends_at"]) - datetime.datetime.utcnow()).total_seconds()
    print(f"Mid-period: price met by the sum but nothing awarded; waiting {max(0, wait):.1f}s for period 1 to end on the real clock")
    time.sleep(max(0, wait) + 0.3)
    s = state()   # a plain read triggers the lazy processing
    won = [a for a in s["awards"] if a["draftable_asset_id"] == A]
    check(len(won) == 1 and won[0]["team_id"] == x and won[0]["period_number"] == 1
          and won[0]["reason"] == "threshold", f"period 1 result wrong: {won}")
    check(won[0]["price"] is None, "another team's bid amount must not be visible")
    check(db.query(RecruitingAward).filter_by(draft_id=did, draftable_asset_id=A).one().price == T - 3,
          "winner should be charged their own bid")
    check(all(a["price"] is None for a in s["awards"] if a["team_id"] != my_team), "a rival's bid amount leaked")
    check(all(a["price"] is None for a in client.get(f"/recruiting/{did}/state").json()["awards"]),
          "anonymous caller must see no bid amounts at all")
    check(s["current_period"] == 2, "should now be period 2")
    check(bid(my_team, A, 5).status_code == 400, "bidding on a recruited player must be rejected")
    fin = {t["team_id"]: t for t in s["teams"]}
    for t in s["teams"]:   # money accumulates: week 2 has two weeks of budget
        check(t["weeks_funded"] == 2 and t["available"] == 2 * t["weekly_budget"], f"budget didn't accumulate: {t}")
    check(fin[x]["spent"] == T - 3 and fin[x]["lost_on_bids"] == 0, f"winner charge wrong: {fin[x]}")
    check(fin[y]["spent"] == 2 and fin[y]["lost_on_bids"] == 2, f"losing bidder must lose its $2 bid: {fin[y]}")
    check(fin[my_team]["spent"] == 1 and fin[my_team]["lost_on_bids"] == 1, f"human's losing $1 bid must be charged: {fin[my_team]}")
    mine = [c for c in s["my_charges"] if c["draftable_asset_id"] == A]
    check(len(mine) == 1 and mine[0]["amount"] == 1 and not mine[0]["is_winner"], f"my_charges wrong: {mine}")
    print(f"Period 1 ended on its own: highest bidder (team {x}) won at their own ${T - 3}, not the ${T} price; "
          f"the two losing bidders still lost their $2 and $1 bids; now period 2")

    # ---- the buffer: results are out, nobody can bid ------------------------------------
    st_ = client.get(f"/recruiting/league/{lid}").json()
    check(st_["phase"] == "buffer" and st_["period_ends_at"] is None and st_["next_period_starts_at"] is not None
          and st_["buffer_seconds"] == BUFFER_SECONDS, f"should be in the buffer: {st_}")
    r_ = bid(my_team, F, 15)
    check(r_.status_code == 400 and "paused" in r_.text, f"a bid during the buffer must be rejected: {r_.text}")
    check(bid(my_team, F, 0).status_code == 400, "retracting during the buffer must be rejected too")
    check(client.post(f"/recruiting/{did}/ai/tick?intensity=1.0").json()["bids_placed"] == 0, "the AI must not bid in the buffer")
    check(process_now().status_code == 400, "can't process a week that hasn't opened yet")
    print(f"Buffer: phase=buffer, a bid and a retract were both rejected ('paused between weeks'), the AI sat it out")

    # ---- the end-of-week results screen ---------------------------------------------------
    check(client.get(RP, params={"team_id": my_team}).status_code == 401, "results need a token")
    check(client.get(RP, params={"team_id": my_team}, headers=other).status_code == 403, "another user must not read my results")
    pend = client.get(RP, params={"team_id": my_team}, headers=me).json()
    check([q["period_number"] for q in pend] == [1], f"exactly week 1 should be waiting: {[q['period_number'] for q in pend]}")
    res1 = pend[0]
    rA = next(b for b in res1["bids"] if b["draftable_asset_id"] == A)
    rF = next(b for b in res1["bids"] if b["draftable_asset_id"] == F)
    check(rA["outcome"] == "lost" and rA["charged"] == 1 and rA["bid_amount"] == 1 and rA["completion_pct"] == 100.0, f"A result: {rA}")
    check(rF["outcome"] == "pending" and rF["charged"] == 0 and rF["completion_pct"] == round(100.0 * 10 / price[F], 1),
          f"F result should be pending at {round(100.0 * 10 / price[F], 1)}%: {rF}")
    check(res1["charged_total"] == 1 and res1["lost_total"] == 1 and res1["players_won"] == 0 and not res1["is_final"], f"summary: {res1}")
    # the completion percentage lives on this screen only
    for path in (f"/recruiting/{did}/state", f"/recruiting/{did}/pool", f"/recruiting/league/{lid}"):
        check("completion" not in client.get(path, headers=me).text.lower(), f"completion % leaked via {path}")
    # closing it
    check(client.post(f"/recruiting/{did}/results/ack", json={"team_id": my_team, "period_number": 1}, headers=other).status_code == 403,
          "another user can't close my results")
    check(client.post(f"/recruiting/{did}/results/ack", json={"team_id": my_team, "period_number": 5}, headers=me).status_code == 400,
          "can't acknowledge a week that hasn't been processed")
    check(client.get(RP, params={"team_id": my_team}, headers=me).json()[0]["period_number"] == 1, "results stay until closed")
    check(client.post(f"/recruiting/{did}/results/ack", json={"team_id": my_team, "period_number": 1}, headers=me).status_code == 200, "ack failed")
    check(client.get(RP, params={"team_id": my_team}, headers=me).json() == [], "closed results must not come back")
    print(f"Results screen: A lost (charged $1, 100% complete), F not recruited (carries over, {rF['completion_pct']}% complete); "
          f"percentages appear nowhere else; the screen stays until closed")
    wait_buffer()
    st_ = client.get(f"/recruiting/league/{lid}").json()
    check(st_["phase"] == "bidding" and st_["current_period"] == 2 and st_["period_ends_at"] is not None, f"week 2 should be open: {st_}")
    check(bid(my_team, F, 15).status_code == 200, "bidding must resume when week 2 opens")

    # ---- carry-over: a short bid stays, later bids complete it -----------------
    C = next(a for a in price if a not in (A, B, F) and 100 <= price[a] <= 280)
    Tc = price[C]
    recruiting.place_bid(db, did, x, C, Tc - 3)
    recruiting.place_bid(db, did, y, C, 1)
    db.commit()
    check(process_now().status_code == 200, "process-period failed")          # ends period 2
    wait_buffer()
    check(all(a["draftable_asset_id"] != C for a in state()["awards"]), "short of the price yet awarded")
    check(db.query(RecruitingBid).filter_by(draft_id=did, draftable_asset_id=C).count() == 2, "short bids didn't carry over")
    check(bid(my_team, C, 3).status_code == 200, "bid rejected")               # sum Tc-3+1+3 = Tc+1
    check(process_now().status_code == 200, "process-period failed")          # ends period 3
    wait_buffer()
    got = [a for a in state()["awards"] if a["draftable_asset_id"] == C]
    check(len(got) == 1 and got[0]["team_id"] == x and got[0]["period_number"] == 3,
          f"carry-over award wrong: {got}")
    check(db.query(RecruitingAward).filter_by(draft_id=did, draftable_asset_id=C).one().price == Tc - 3, "carry-over charge wrong")
    # the budget panel's numbers, live
    for t in state()["teams"]:
        check(t["recruiting_budget_total"] == 20 * t["weekly_budget"], f"Recruiting Budget (Total) = weekly x 20 weeks: {t}")
        check(t["season_budget_total"] == 10 * t["weekly_budget"] + t["recruiting_budget_total"], f"Season Budget (Total): {t}")
        if t["team_id"] == my_team:
            check(t["roster_cost"] is not None and t["season_weekly_budget"] ==
                  round((t["season_budget_total"] - t["roster_cost"]) / 10), f"Weekly Budget = (season - roster cost) / 10: {t}")
        else:
            check(t["roster_cost"] is None and t["season_weekly_budget"] is None, "another team's roster cost must stay private")
    fin3 = {t["team_id"]: t for t in state()["teams"]}
    check(all(t["weeks_funded"] == 4 and t["available"] == 4 * t["weekly_budget"] for t in fin3.values()), "budget not accumulating")
    check(db.query(RecruitingCharge).filter_by(draft_id=did, draftable_asset_id=C).count() == 3,
          "all three bidders on the carried-over player should have been charged")
    print(f"Carry-over: bids ${Tc - 3}+$1 fell short of ${Tc} in period 2 and stayed; a $3 bid in period 3 completed it")

    # ---- free-for-all bidding, period by period -----------------------------------
    ranked = sorted(pool, key=lambda p: value.get(p["draftable_asset_id"], 0), reverse=True)
    t0 = time.time()
    saw_final = False
    while True:
        s = state()
        if s["status"] != "open":
            break
        n_before = len(s["awards"])
        this_period = s["current_period"]
        for _ in range(TICKS_PER_PERIOD):
            r = client.post(f"/recruiting/{did}/ai/tick?intensity=1.0")
            check(r.status_code == 200, r.text)
            s = state()
            check(
                s["status"] != "open"
                or s.get("current_period") != this_period
                or s.get("phase") == "buffer"
                or len(s["awards"]) == n_before,
                "awards appeared mid-period",
            )
            taken = {a["draftable_asset_id"] for a in s["awards"]}
            mine_now = {b["draftable_asset_id"]: b["amount"] for b in s["my_bids"]}
            have = sum(1 for a in s["awards"] if a["team_id"] == my_team)
            if have < ROSTER_SIZE:
                if len(mine_now) < 6:
                    pick = next((p for p in ranked[:80] if p["draftable_asset_id"] not in taken
                                 and p["draftable_asset_id"] not in mine_now), None)
                    if pick:
                        bid(my_team, pick["draftable_asset_id"], rng.randint(40, 120))
                for aid_, amt in list(mine_now.items()):
                    if amt < 400:
                        bid(my_team, aid_, amt + rng.randint(10, 40))
        r = process_now()
        check(r.status_code == 200, r.text)
        s = state()
        # the team closes each week's results screen before the next week
        for q in client.get(RP, params={"team_id": my_team}, headers=me).json():
            saw_final = saw_final or q["is_final"]
            check(client.post(f"/recruiting/{did}/results/ack", json={"team_id": my_team, "period_number": q["period_number"]},
                              headers=me).status_code == 200, "ack failed")
        wait_buffer()
        check(all(t["spent"] <= t["available"] for t in s["teams"]), f"a team overspent: {[t for t in s['teams'] if t['spent'] > t['available']]}")
        print(f"  period {this_period} processed: {len(s['awards'])} recruited so far, {time.time() - t0:.0f}s")
    s = state()
    print(f"Draft closed after {db.get(RecruitingDraft, did).periods_processed} periods: {len(s['awards'])} recruited")
    check(bid(my_team, any_asset, 1).status_code == 400, "bidding after the draft closed must be rejected")
    check(saw_final, "the final week's results screen should have been shown")
    check(client.get(RP, params={"team_id": my_team}, headers=me).json() == [], "every results screen should have been closed")

    # ---- audit against raw rows ---------------------------------------------------
    db.expire_all()
    draft = db.get(RecruitingDraft, did)
    awards = db.query(RecruitingAward).filter_by(draft_id=did).all()
    bids_by_asset = defaultdict(list)
    for b in db.query(RecruitingBid).filter_by(draft_id=did):
        bids_by_asset[b.draftable_asset_id].append(b)
    counts = Counter(a.team_id for a in awards)
    check(max(counts.values()) <= ROSTER_SIZE, "a roster exceeded the cap")
    check(len({a.draftable_asset_id for a in awards}) == len(awards), "a player was recruited twice")
    entries = db.query(RosterEntry).filter(RosterEntry.acquired_via == "recruiting",
                                           RosterEntry.team_id.in_([t["id"] for t in teams])).all()
    check(len(entries) == len(awards), "roster_entries != awards")
    check(draft.status == "closed" and (draft.periods_processed == draft.total_periods
                                        or all(n >= ROSTER_SIZE for n in counts.values())),
          "draft closed before its last period without full rosters")
    ties = deadline = 0
    for a in awards:
        bids = bids_by_asset.get(a.draftable_asset_id, [])
        check(bids, f"asset {a.draftable_asset_id} awarded with no bids on record")
        top = max(b.amount for b in bids)
        win = next(b for b in bids if b.team_id == a.team_id)
        capped_out = [b for b in bids if b.amount > win.amount and counts[b.team_id] >= ROSTER_SIZE]
        check(win.amount == top or capped_out, f"asset {a.draftable_asset_id}: winner isn't the highest bidder")
        check(a.price == win.amount, "winner not charged their own bid")
        check(1 <= a.period_number <= draft.total_periods, "bad period number")
        if a.reason == "threshold":
            check(sum(b.amount for b in bids) >= price[a.draftable_asset_id], "threshold award below the price")
        else:
            check(a.period_number == draft.total_periods, "deadline award before the final period")
            deadline += 1
        ties += a.was_tie_broken_randomly
    charges = db.query(RecruitingCharge).filter_by(draft_id=did).all()
    by_charge = defaultdict(list)
    for c in charges:
        by_charge[c.draftable_asset_id].append(c)
    check(set(by_charge) == {a.draftable_asset_id for a in awards}, "charges exist for an unawarded player (or vice versa)")
    for a in awards:
        cs = by_charge[a.draftable_asset_id]
        check(sum(1 for c in cs if c.is_winner) == 1 and next(c for c in cs if c.is_winner).team_id == a.team_id,
              "exactly the winner must be flagged as winner")
        bid_amt = {b.team_id: b.amount for b in bids_by_asset[a.draftable_asset_id]}
        check(all(c.amount == bid_amt[c.team_id] for c in cs), "a bidder wasn't charged exactly its own bid")
    lost_total = sum(c.amount for c in charges if not c.is_winner)
    results = db.query(RecruitingBidResult).filter_by(draft_id=did).all()
    check(sorted((r.team_id, r.draftable_asset_id, r.bid_amount) for r in results if r.outcome in ("won", "lost"))
          == sorted((c.team_id, c.draftable_asset_id, c.amount) for c in charges), "won/lost results don't match the charges")
    check(all(r.charged == 0 and r.completion_pct < 100 for r in results if r.outcome == "pending"), "a pending result was charged or complete")
    check(all(r.completion_pct >= 100 for r in results if r.outcome in ("won", "lost") and r.period_number < draft.total_periods),
          "a player awarded before the deadline must have been 100% complete")
    check(all(r.draftable_asset_id in {a.draftable_asset_id for a in awards} for r in results if r.outcome in ("won", "lost")), "result for an unawarded player")
    per_period = Counter(a.period_number for a in awards)
    finances = {t["team_id"]: t for t in s["teams"]}
    print(f"\n{'team':<24}{'tier':<10}{'weekly':>7}{'avail':>7}{'spent':>7}{'lost':>6}{'left':>6}{'season':>8}{'roster':>7}   owner")
    for t in teams:
        f = finances[t["id"]]
        check(f["spent"] == sum(c.amount for c in charges if c.team_id == t["id"]), "spent != sum of the team's charges")
        check(f["lost_on_bids"] == sum(c.amount for c in charges if c.team_id == t["id"] and not c.is_winner), "lost_on_bids wrong")
        check(f["weekly_budget"] == STARTING_BUDGET_BY_PROGRAM_SIZE[t["program_size"]], "weekly budget doesn't follow program size")
        check(f["available"] == f["weekly_budget"] * f["weeks_funded"] and f["weeks_funded"] == draft.total_periods,
              "available money should be the weekly budget x the weeks of the recruitment")
        check(f["remaining"] == f["available"] - f["spent"] and f["remaining"] >= 0, f"team overspent: {f}")
        check(f["recruiting_budget_total"] == draft.total_periods * f["weekly_budget"], "recruiting total wrong")
        check(f["season_budget_total"] == 10 * f["weekly_budget"] + f["recruiting_budget_total"], "season total wrong")
        won_cost = sum(c.amount for c in charges if c.team_id == t["id"] and c.is_winner)
        if not t["is_ai_owned"]:   # roster cost / weekly season budget are private to the owner
            check(f["roster_cost"] == won_cost, f"roster cost should be what was paid for the roster: {f['roster_cost']} vs {won_cost}")
            check(f["season_weekly_budget"] == round((f["season_budget_total"] - won_cost) / 10), "weekly season budget wrong")
            check(f["roster_cost"] <= f["spent"], "roster cost can't exceed everything charged")
        print(f"{t['name']:<24}{t['program_size']:<10}{f['weekly_budget']:>7}{f['available']:>7}{f['spent']:>7}{f['lost_on_bids']:>6}{f['remaining']:>6}{f['season_budget_total']:>8}"
              f"{f['roster_count']:>7}   {'AI' if t['is_ai_owned'] else 'human'}")
    print(f"\n{len(awards)} players recruited over {len(per_period)} periods ({deadline} at the final-period deadline); "
          f"{ties} random tie-breaks; ${lost_total} was lost on bids for players the bidder didn't get")
    print("Recruited per period:", dict(sorted(per_period.items())))
    print("MOCK RECRUITING DRAFT PASSED")


if __name__ == "__main__":
    main()
