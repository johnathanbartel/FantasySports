"""
End-to-end check of SCORING: an all-AI league is drafted, given lineups and a
schedule for the first scored weeks, and every number on the results/standings
side is audited against the raw per-player scores.

  * the higher team score wins each matchup; ties are draws
  * one team's weekly total equals the sum of its STARTERS' weekly_player_scores
    (bench ignored), read straight from the table - not through the scoring code
  * standings' wins/losses/points_for add up from the matchups
  * a retroactive lineup swap (locked past week, commissioner) changes that team's
    score by exactly the difference of the two players' scores, and the results
    and standings update
  * lock: a human owner can't edit a started week, but can edit one that hasn't started

Usage (repo root; needs user_run.db with the current season's weeks scored):
    .venv-api/Scripts/python.exe scripts/run_mock_scoring.py

Runs on a throwaway copy of user_run.db (mock_score_run.db). With --keep-live it instead
builds the test league IN user_run.db (what the demo server serves) and stops.
"""

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
LIVE_DB, RUN_DB = ROOT / "user_run.db", ROOT / "mock_score_run.db"
KEEP_LIVE = "--keep-live" in sys.argv
if not LIVE_DB.exists():
    sys.exit("user_run.db missing")
if not KEEP_LIVE:
    shutil.copyfile(LIVE_DB, RUN_DB)
target = LIVE_DB if KEEP_LIVE else RUN_DB
os.environ["DATABASE_URL"] = f"sqlite:///{target.as_posix()}"
os.environ.pop("FSC_GRANT_ALL_PREMIUM", None)

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import entitlements, scoring
from fantasy_sports.platform.db.models import core  # noqa: F401
from fantasy_sports.platform.db.models.core import LineupEntry, Team, User, Week
from fantasy_sports.platform.db.session import SessionLocal
from fantasy_sports.sports.football.models import WeeklyPlayerScore

TEST_WEEKS = 3


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    client = TestClient(app)
    db = SessionLocal()
    users = db.query(User).order_by(User.id).all()
    boss, other = users[0], users[1]
    entitlements.grant(db, boss.id, entitlements.COMMISSIONER_MODE)
    db.commit()

    def login(uid):
        r = client.post("/sessions", json={"user_id": uid})
        check(r.status_code == 200, r.text)
        return {"Authorization": "Bearer " + r.json()["token"]}

    B, O = login(boss.id), login(other.id)

    league = client.post("/leagues/sandbox", json={"name": "Scoring Test League", "recruiting_period_seconds": 3600}, headers=B).json()
    lid = league["id"]
    t0 = time.time()
    r = client.post(f"/recruiting/league/{lid}/simulate", headers=B)
    check(r.status_code == 200 and r.json()["status"] == "closed", r.text)
    print(f"League {lid}: 12 AI teams drafted ({time.time() - t0:.0f}s)")

    weeks = client.get(f"/leagues/{lid}/weeks").json()
    scored = [w for w in weeks if w["scored"]]
    check(len(scored) >= TEST_WEEKS and [w["week_number"] for w in scored][:TEST_WEEKS] == [1, 2, 3], f"weeks 1-3 should be scored: {weeks[:5]}")
    check(all(w["start_date"] for w in weeks[:4]), "weeks should have start dates")
    check(scored[0]["start_date"] == "2026-09-03", "week 1 should start 2026-09-03")
    test_weeks = scored[:TEST_WEEKS]
    for w in test_weeks:
        r = client.post(f"/leagues/{lid}/lineups/auto", params={"week_id": w["id"]}, headers=B)
        check(r.status_code == 200, r.text)
    r = client.post(f"/leagues/{lid}/schedule/auto", json={"weeks": 9}, headers=B)
    check(r.status_code == 200 and r.json()["matchups"] == 6 * 9, r.text)
    check(client.post(f"/leagues/{lid}/schedule/auto", json={"weeks": 9}, headers=B).status_code == 400, "re-scheduling needs replace")
    check(client.post(f"/leagues/{lid}/schedule/auto", json={"weeks": 9}, headers=O).status_code == 403, "only the commissioner schedules")
    print("Lineups set for weeks 1-3; round-robin schedule created (54 matchups)")

    db.expire_all()
    matchups = client.get(f"/leagues/{lid}/matchups").json()
    check(len(matchups) == 54, len(matchups))
    check(all(m["scored"] for m in matchups if m["week_id"] in {w["id"] for w in test_weeks}), "weeks 1-3 should be scored")
    check(all(not m["scored"] and m["team_a_score"] is None for m in matchups if m["week_id"] not in {w["id"] for w in test_weeks}),
          "unplayed weeks must not show scores")

    # winner = higher score; every team plays once a week
    for m in matchups:
        if m["scored"]:
            a, b = m["team_a_score"], m["team_b_score"]
            want = m["team_a_id"] if a > b else m["team_b_id"] if b > a else None
            check(m["winner_team_id"] == want, f"winner wrong in matchup {m['matchup_id']}: {a} vs {b}")
            ra, rb = scoring.team_raw_credit(db, m["team_a_id"], m["week_id"]), scoring.team_raw_credit(db, m["team_b_id"], m["week_id"])
            check(ra == rb or (ra > rb) == (a > b), f"the EPA-credit leader must win: credit {ra} vs {rb}, points {a} vs {b}")
    for w in test_weeks:
        ids = [t for m in matchups if m["week_id"] == w["id"] for t in (m["team_a_id"], m["team_b_id"])]
        check(sorted(ids) == sorted(set(ids)) and len(ids) == 12, f"week {w['week_number']}: not every team plays once")

    # one team's week total == sum of its starters' rows in weekly_player_scores (raw table read)
    def raw_total(team_id, week_id):
        starters = [e.draftable_asset_id for e in db.query(LineupEntry).filter_by(team_id=team_id, week_id=week_id) if e.roster_slot.is_starter]
        rows = db.query(WeeklyPlayerScore).filter(WeeklyPlayerScore.week_id == week_id, WeeklyPlayerScore.draftable_asset_id.in_(starters))
        return sum(x.points for x in rows), len(starters)

    m1 = next(m for m in matchups if m["week_id"] == test_weeks[0]["id"])
    total, n = raw_total(m1["team_a_id"], m1["week_id"])
    check(n == 10 or n > 5, f"unexpected starter count {n}")
    ctx = scoring.league_context(db, lid)
    check(ctx.n_teams == 12 and ctx.starter_slots.get("K") == 1 and ctx.starter_slots.get("QB") == 1, f"scale context wrong: {ctx}")
    want = scoring.to_fantasy_points(total, ctx)
    check(abs(want - m1["team_a_score"]) < 1e-9, f"team score {m1['team_a_score']} != scale(sum of starters' credit {total}) = {want}")
    bd = client.get(f"/teams/{m1['team_a_id']}/week-breakdown", params={"week_id": m1["week_id"]}).json()
    check(abs(sum(s["points"] for s in bd["starters"]) - total) < 1e-9 and abs(bd["raw_credit"] - total) < 1e-9 and len(bd["starters"]) == n, "breakdown doesn't add up")
    check(abs(client.get(f"/teams/{m1['team_a_id']}/weekly-score", params={"week_id": m1["week_id"]}).json()["total_points"] - want) < 1e-9,
          "/weekly-score should be the scaled starters-only total")
    check(all(s["points"] >= 0 for s in [{"points": m["team_a_score"]} for m in matchups if m["scored"]]), "fantasy scores must be positive")
    scored_scores = [x for m in matchups if m["scored"] for x in (m["team_a_score"], m["team_b_score"])]
    print(f"Team {m1['team_a_id']} week 1: {m1['team_a_score']:.2f} pts = scale(credit {total:.3f} from {n} starters); "
          f"league weeks 1-3 average {sum(scored_scores) / len(scored_scores):.1f} pts (NCAA season avg 28.1; early weeks run high)")

    # standings add up from the matchups
    def standings():
        return {r["team_id"]: r for r in client.get(f"/leagues/{lid}/standings").json()}

    st = standings()
    for tid, row in st.items():
        mine = [m for m in matchups if m["scored"] and tid in (m["team_a_id"], m["team_b_id"])]
        check(row["wins"] + row["losses"] + row["ties"] == len(mine) == TEST_WEEKS, f"team {tid} record doesn't cover 3 games")
        check(row["wins"] == sum(1 for m in mine if m["winner_team_id"] == tid), f"team {tid} wins wrong")
        pf = sum(m["team_a_score"] if m["team_a_id"] == tid else m["team_b_score"] for m in mine)
        check(abs(row["points_for"] - pf) < 1e-6, f"team {tid} points_for wrong")
    check(sum(r["wins"] for r in st.values()) == sum(r["losses"] for r in st.values()), "league wins != losses")
    print("Standings: every team's record and points tie back to the matchups; wins == losses league-wide")

    if KEEP_LIVE:
        print(f"\nTest league {lid} left in user_run.db (commissioner: user {boss.id}, weeks 1-3 scored, schedule created).")
        return

    # ---- retroactive lineup change ------------------------------------------------------------
    wk = m1["week_id"]
    team_id = m1["team_a_id"]
    cur = client.get(f"/teams/{team_id}/lineup", params={"week_id": wk}).json()
    starters_now = {e["draftable_asset_id"] for e in bd["starters"]}
    bench = [e for e in cur if e["slot_code"] == "BENCH"]
    slot_of = {}
    for e in cur:
        slot_of.setdefault(e["slot_code"], []).append(e)
    slots = {s["slot_code"]: s for s in client.get(f"/leagues/{lid}/roster-slots").json()}
    pos = {r["draftable_asset_id"]: r["position"] for r in client.get(f"/teams/{team_id}/roster").json()}
    # find a starter and a bench player of the same position with different scores
    scores = {x.draftable_asset_id: x.points for x in db.query(WeeklyPlayerScore).filter_by(week_id=wk)}
    swap = None
    for s in bd["starters"]:
        for b in bench:
            if pos[b["draftable_asset_id"]] == s["position"] and s["slot_code"] != "FLEX" and scores.get(b["draftable_asset_id"], 0) != s["points"]:
                swap = (s, b)
                break
        if swap:
            break
    check(swap is not None, "no same-position starter/bench pair to swap")
    s_out, b_in = swap
    assign = {}
    for e in cur:
        code = e["slot_code"]
        aid = e["draftable_asset_id"]
        if aid == s_out["draftable_asset_id"]:
            aid = b_in["draftable_asset_id"]
        elif aid == b_in["draftable_asset_id"]:
            aid = s_out["draftable_asset_id"]
        if slots[code]["slot_count"] > 1:
            assign.setdefault(code, []).append(aid)
        else:
            assign[code] = aid
    r = client.put(f"/teams/{team_id}/lineup", json={"week_id": wk, "assignments": assign}, headers=B)
    check(r.status_code == 200, f"commissioner retro edit failed: {r.text}")
    after = next(m for m in client.get(f"/leagues/{lid}/matchups", params={"week_id": wk}).json() if m["matchup_id"] == m1["matchup_id"])
    delta = scores.get(b_in["draftable_asset_id"], 0.0) - s_out["points"]          # in raw credit
    check(abs(after["team_a_score"] - scoring.to_fantasy_points(total + delta, ctx)) < 1e-9, "score isn't scale(old credit + the swapped players' difference)")
    delta = after["team_a_score"] - m1["team_a_score"]                                # in fantasy points, for the standings check
    check(abs(after["team_b_score"] - m1["team_b_score"]) < 1e-12, "opponent's score changed")
    st2 = standings()
    check(abs(st2[team_id]["points_for"] - (st[team_id]["points_for"] + delta)) < 1e-9, "standings points_for didn't follow the retro swap")
    print(f"Retro swap in locked week 1 ({s_out['display_name']} {s_out['points']:+.3f} -> {b_in['display_name']} "
          f"{scores.get(b_in['draftable_asset_id'], 0.0):+.3f}): team score moved {delta:+.2f} pts, standings followed "
          f"(winner {'unchanged' if after['winner_team_id'] == m1['winner_team_id'] else 'FLIPPED'})")

    # ---- lock: a human owner ---------------------------------------------------------------------
    t = db.get(Team, team_id)
    t.is_ai_owned, t.owner_user_id = False, other.id
    db.commit()
    unlocked = next(w for w in weeks if not w["locked"])
    locked_wk = test_weeks[0]
    check(locked_wk["locked"] and not unlocked["scored"], "test week states wrong")
    r = client.post(f"/teams/{team_id}/lineup/auto", params={"week_id": locked_wk["id"]}, headers=O)
    check(r.status_code == 403, f"an owner must not edit a started week: {r.status_code} {r.text}")
    r = client.post(f"/teams/{team_id}/lineup/auto", params={"week_id": unlocked["id"]}, headers=O)
    check(r.status_code == 200, f"an owner should be able to edit a week that hasn't started: {r.text}")
    print(f"Lock: owner refused on started week 1 (403), allowed on week {unlocked['week_number']} (starts {unlocked['start_date']})")
    print("\nSCORING CHECKS PASSED")


if __name__ == "__main__":
    main()
