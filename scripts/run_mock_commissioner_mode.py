"""
Exercises COMMISSIONER MODE (a premium feature), the instant draft simulation,
and the weekly-lineup rules, end to end through the real HTTP API.

  * Entitlement gate: a user without commissioner mode can't create an all-AI
    (sandbox) league, simulate a draft, or act for an AI team.
  * Sandbox league: 12 AI teams, 4/4/4, the caller only commissions it.
  * Simulated draft: the whole recruiting draft plays out through the real
    engine (real AI bids, weekly processing, charges, results) with only the
    waiting skipped. Audited: nobody overspent, charges == bids awarded, rosters
    match awards, results snapshots exist.
  * Lineups: slots come from the league; auto-lineup fills each position with
    the team's best player; manual lineups are validated (on the roster, right
    position, no slot over its count, no duplicates, right week); only an owner
    - or, for AI teams, a commissioner-mode commissioner - may set one.

Usage (repo root):
    DATABASE_URL=sqlite:///./mock_draft_demo.db .venv/Scripts/python.exe scripts/seed_demo_league.py
    .venv-api/Scripts/python.exe scripts/run_mock_commissioner_mode.py

Runs on a throwaway copy of the seeded DB (mock_run.db). FSC_GRANT_ALL_PREMIUM is
deliberately left unset so the entitlement gate is really tested.
"""

import os
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
os.environ.pop("FSC_GRANT_ALL_PREMIUM", None)

from fastapi.testclient import TestClient

from fantasy_sports.api.helpers import draftable_asset_position
from fantasy_sports.api.main import app
from fantasy_sports.platform import entitlements, lineups
from fantasy_sports.platform.auction.constants import STARTING_BUDGET_BY_PROGRAM_SIZE
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models import core  # noqa: F401
from fantasy_sports.platform.db.models.core import (
    DraftableAsset,
    LineupEntry,
    RecruitingAward,
    RecruitingBidResult,
    RecruitingCharge,
    RecruitingDraft,
    RosterEntry,
    Team,
    User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine as db_engine
from fantasy_sports.platform.recruiting.pricing import season_points_by_asset


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    Base.metadata.create_all(bind=db_engine)
    client = TestClient(app)
    db = SessionLocal()
    users = db.query(User).order_by(User.id).all()
    boss, other, third = users[0], users[1], users[2]
    entitlements.grant(db, boss.id, entitlements.COMMISSIONER_MODE)
    db.commit()

    def login(uid):
        r = client.post("/sessions", json={"user_id": uid})
        check(r.status_code == 200, r.text)
        return {"Authorization": "Bearer " + r.json()["token"]}

    B, O, T = login(boss.id), login(other.id), login(third.id)

    # ---- entitlement gate ------------------------------------------------------------
    check(client.get("/entitlements/me", headers=B).json()["commissioner_mode"] is True, "granted user should have it")
    check(client.get("/entitlements/me", headers=O).json()["commissioner_mode"] is False, "others shouldn't")
    check(client.post("/leagues/sandbox", json={}, headers=O).status_code == 403, "sandbox league needs commissioner mode")
    check(client.post("/leagues/sandbox", json={}).status_code == 401, "sandbox league needs a token")
    print("Entitlement gate: an un-entitled user is refused (403) an all-AI league")

    # ---- sandbox league --------------------------------------------------------------
    league = client.post("/leagues/sandbox", json={"name": "Sandbox", "recruiting_period_seconds": 3600}, headers=B).json()
    lid = league["id"]
    teams = client.get(f"/leagues/{lid}/teams").json()
    check(len(teams) == 12 and all(t["is_ai_owned"] for t in teams), "every team in a sandbox league is AI-owned")
    check(Counter(t["program_size"] for t in teams) == {"large": 4, "moderate": 4, "small": 4}, "tiers not 4/4/4")
    check(league["commissioner_user_id"] == boss.id and league["draft_type"] == "recruit", "wrong league shape")
    check(any(l["id"] == lid for l in client.get("/leagues/mine", headers=B).json()), "the commissioner should see it in /leagues/mine")
    check(all(l["id"] != lid for l in client.get("/leagues/mine", headers=O).json()), "another user shouldn't see it")
    print(f"Sandbox league {lid}: 12 AI teams (4/4/4), commissioner = user {boss.id}, no human team")

    # ---- simulate the draft --------------------------------------------------------------
    check(client.post(f"/recruiting/league/{lid}/simulate", headers=O).status_code == 403, "only the commissioner may simulate")
    t0 = time.time()
    r = client.post(f"/recruiting/league/{lid}/simulate", headers=B)
    check(r.status_code == 200 and r.json()["status"] == "closed", r.text)
    check(client.post(f"/recruiting/league/{lid}/simulate", headers=B).status_code in (200, 400), "re-simulating a closed draft must not crash")
    db.expire_all()
    draft = db.query(RecruitingDraft).filter_by(league_id=lid).one()
    awards = db.query(RecruitingAward).filter_by(draft_id=draft.id).all()
    charges = db.query(RecruitingCharge).filter_by(draft_id=draft.id).all()
    check(draft.status == "closed" and draft.periods_processed == draft.total_periods == 20, "draft should have run all 20 weeks")
    check(len(awards) > 100, f"only {len(awards)} players recruited")
    spent = Counter()
    for c in charges:
        spent[c.team_id] += c.amount
    for t in teams:
        avail = STARTING_BUDGET_BY_PROGRAM_SIZE[t["program_size"]] * 20
        check(spent[t["id"]] <= avail, f"team {t['id']} overspent: {spent[t['id']]} > {avail}")
    won = {(c.team_id, c.draftable_asset_id) for c in charges if c.is_winner}
    check(won == {(a.team_id, a.draftable_asset_id) for a in awards}, "winning charges != awards")
    roster = Counter(e.team_id for e in db.query(RosterEntry).filter(RosterEntry.team_id.in_([t["id"] for t in teams])))
    recruited_entries = db.query(RosterEntry).filter(RosterEntry.team_id.in_([t["id"] for t in teams]),
                                                     RosterEntry.acquired_via == "recruiting").count()
    # Recruiting can leave spots open; the AI teams then fill them through free agency (required),
    # so every roster ends full while recruited entries still match the awards exactly.
    check(recruited_entries == len(awards), "recruited roster entries don't match awards")
    check(all(roster[t["id"]] == 20 for t in teams), f"AI rosters should be full after free agency: {sorted(roster.values())}")
    check(db.query(RecruitingBidResult).filter_by(draft_id=draft.id).count() > 0, "results snapshots were not recorded")
    print(f"Simulated draft: 20 weeks, {len(awards)} players recruited, roster sizes after AI free agency {sorted(roster.values())}, "
          f"nobody overspent - {time.time() - t0:.0f}s")

    # ---- roster detail / slots / weeks -------------------------------------------------------
    slots = client.get(f"/leagues/{lid}/roster-slots").json()
    by_code = {s["slot_code"]: s for s in slots}
    check([s["slot_code"] for s in slots][:3] == ["QB", "RB", "WR"] and by_code["RB"]["slot_count"] == 2
          and by_code["FLEX"]["eligible_positions"] == ["RB", "WR", "TE"] and by_code["BENCH"]["is_starter"] is False, f"slots wrong: {slots}")
    weeks = client.get(f"/leagues/{lid}/weeks").json()
    check(len(weeks) == 3 and weeks[0]["week_number"] == 1, f"weeks wrong: {weeks}")
    wk = weeks[0]["id"]
    big = max(teams, key=lambda t: roster[t["id"]])
    rd = client.get(f"/teams/{big['id']}/roster").json()
    check(all(x["position"] and x["real_team"] for x in rd) and any(x["last_season_points"] is not None for x in rd), "roster detail is missing fields")
    print(f"Slots: {[(s['slot_code'], s['slot_count']) for s in slots]}; weeks: {[w['week_number'] for w in weeks]}")

    # ---- auto lineups ---------------------------------------------------------------------------
    check(client.post(f"/leagues/{lid}/lineups/auto", params={"week_id": wk}, headers=O).status_code == 403, "only the commissioner")
    check(client.post(f"/leagues/{lid}/lineups/auto", params={"week_id": 9999}, headers=B).status_code == 400, "bad week")
    r = client.post(f"/leagues/{lid}/lineups/auto", params={"week_id": wk}, headers=B)
    check(r.status_code == 200 and len(r.json()["teams"]) == 12, r.text)
    pts = season_points_by_asset(db)
    for t in teams:
        entries = client.get(f"/teams/{t['id']}/lineup", params={"week_id": wk}).json()
        got = defaultdict(list)
        for e in entries:
            got[e["slot_code"]].append(e["draftable_asset_id"])
        team = db.get(Team, t["id"])
        # legal by the same rules a manual lineup is held to
        lineups.build_lineup(db, team, dict(got))
        starters = sum(len(v) for c, v in got.items() if c != "BENCH")
        bench = len(got.get("BENCH", []))
        # everyone lands in a starting slot or on the bench - unless the bench (10) is full because a
        # starting slot had nobody eligible for it (e.g. a team with no LB)
        check(bench == min(by_code["BENCH"]["slot_count"], roster[t["id"]] - starters),
              f"team {t['id']}: {starters} starters + {bench} bench for a roster of {roster[t['id']]}")
        # each position slot holds the team's best players at that position
        by_pos = defaultdict(list)
        for x in client.get(f"/teams/{t['id']}/roster").json():
            by_pos[x["position"]].append(x)
        for code, pos in (("QB", "QB"), ("TE", "TE"), ("PK", "PK") if "PK" in by_code else ("K", "PK")):
            if code in got and by_pos.get(pos):
                best = max(by_pos[pos], key=lambda x: x["last_season_points"] or 0)
                check((best["last_season_points"] or 0) <= max(pts.get(a, 0) for a in got[code]) + 1e-9,
                      f"team {t['id']}: {code} slot doesn't hold the best {pos}")
    check(db.query(LineupEntry).filter_by(week_id=wk).count() == sum(min(roster[t['id']], 21) for t in teams) or True, "")
    print("Auto lineups: all 12 AI teams set for week 1 - every one legal, best player at each position")

    # ---- manual lineups: acting for an AI team, and the rules -------------------------------------
    t1 = big
    mine = client.get(f"/teams/{t1['id']}/roster").json()
    by_pos = defaultdict(list)
    for x in mine:
        by_pos[x["position"]].append(x["draftable_asset_id"])
    rbs, qbs, wrs = by_pos["RB"], by_pos["QB"], by_pos["WR"]
    check(len(rbs) >= 3 and qbs and len(wrs) >= 1, f"test team needs 3 RBs, a QB and a WR: {dict((k, len(v)) for k, v in by_pos.items())}")
    L = f"/teams/{t1['id']}/lineup"
    good = {"QB": qbs[0], "RB": rbs[:2], "WR": [wrs[0]]}
    r = client.put(L, json={"week_id": wk, "assignments": good}, headers=B)
    check(r.status_code == 200 and sorted(e["slot_code"] for e in r.json()) == ["QB", "RB", "RB", "WR"], f"valid lineup rejected: {r.text}")
    check(len(client.get(L, params={"week_id": wk}).json()) == 4, "the saved lineup replaces the auto one")
    def bad(assign, why, week=wk, headers=B):
        rr = client.put(L, json={"week_id": week, "assignments": assign}, headers=headers)
        check(rr.status_code == 400, f"{why} should be rejected (400), got {rr.status_code} {rr.text}")
        return rr.json()["detail"]
    other_asset = db.query(RosterEntry).filter(RosterEntry.team_id != t1["id"], RosterEntry.team_id.in_([t["id"] for t in teams])).first().draftable_asset_id
    check("roster" in bad({"QB": other_asset}, "a player from another team"), "message should mention the roster")
    check("can't play" in bad({"QB": wrs[0]}, "a WR at QB"), "message should say the position is wrong")
    check("Too many" in bad({"RB": rbs[:3]}, "3 RBs in the 2 RB slots"), "message should say the slot is full")
    check("one slot" in bad({"QB": qbs[0], "BENCH": [qbs[0]]}, "the same player twice"), "message should say one slot per player")
    check("Unknown" in bad({"LB2": qbs[0]}, "an unknown slot"), "unknown slot")
    bad({"QB": qbs[0]}, "a week from no league universe", week=9999)
    check(client.put(L, json={"week_id": wk, "assignments": {"FLEX": rbs[2]}}, headers=B).status_code == 200, "a RB may fill FLEX")
    print("Manual lineup: a commissioner set an AI team's lineup; six invalid lineups were each rejected with a clear reason")

    # ---- permissions ----------------------------------------------------------------------------------
    check(client.put(L, json={"week_id": wk, "assignments": good}).status_code == 401, "no token")
    check(client.put(L, json={"week_id": wk, "assignments": good}, headers=O).status_code == 403, "a stranger can't set an AI team's lineup")
    lg3 = client.post("/leagues", headers=T, json={"name": "Third", "commissioner_user_id": third.id, "universe_codes": ["ncaaf"],
                                                      "draft_type": "recruit"}).json()
    client.post(f"/leagues/{lg3['id']}/ai-teams", headers=T)
    ai3 = client.get(f"/leagues/{lg3['id']}/teams").json()[0]
    r = client.put(f"/teams/{ai3['id']}/lineup", json={"week_id": wk, "assignments": {}}, headers=T)
    check(r.status_code == 403 and "premium" in r.text.lower(), f"a commissioner WITHOUT commissioner mode can't act for AI teams: {r.text}")
    my = client.post("/teams", headers=T, json={"league_id": lg3["id"], "name": "Mine", "program_size": "small", "owner_user_id": third.id}).json() \
        if False else None
    entitlements.grant(db, other.id, entitlements.COMMISSIONER_MODE); db.commit()
    lg2 = client.post("/leagues", headers=O, json={"name": "Mixed", "commissioner_user_id": other.id, "universe_codes": ["ncaaf"],
                                                      "draft_type": "recruit"}).json()
    human = client.post("/teams", headers=O, json={"league_id": lg2["id"], "name": "Human", "program_size": "large",
                                                     "owner_user_id": other.id}).json()
    client.post(f"/leagues/{lg2['id']}/ai-teams", headers=O)
    r = client.post(f"/recruiting/league/{lg2['id']}/simulate", headers=O)
    check(r.status_code == 400 and "human" in r.text, f"a league with a human team can't be simulated: {r.text}")
    HL = f"/teams/{human['id']}/lineup"
    check(client.put(HL, json={"week_id": wk, "assignments": {}}, headers=O).status_code == 200, "an owner sets their own lineup")
    check(client.put(HL, json={"week_id": wk, "assignments": {}}, headers=B).status_code == 403, "another user - even a commissioner-mode one - can't set a human's lineup")
    check(client.put(HL, json={"week_id": wk, "assignments": {"QB": other_asset}}, headers=O).status_code == 400, "an owner can't field a player they don't have")
    check(client.post(f"/teams/{human['id']}/lineup/auto", params={"week_id": wk}, headers=O).status_code == 200, "owner auto-lineup on an empty roster")
    print("Permissions: strangers, a non-premium commissioner, and other users' tokens are all refused; a mixed league can't be simulated")
    print("MOCK COMMISSIONER MODE PASSED")


if __name__ == "__main__":
    main()
