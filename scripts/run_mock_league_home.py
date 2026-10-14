"""
Exercises everything behind the league home page's tabs through the real HTTP
API: the overview, free agency (the AI's REQUIRED fill when recruiting closes,
signings/releases, asking prices, money limits, permissions), the transactions
log, and the stats table.

Rules under test (commissioner, 2026-09-20): teams may recruit fewer players
than the roster size; the open spots are filled through free agency, which
opens when recruiting ends; AI teams must use it to fill their roster - each
signs the best free agent it can afford (starting positions it lacks first), and the
cheapest available if it can't afford one, so its roster still fills. The
asking-price rule itself is PROVISIONAL (see platform/free_agency.py).

Usage (repo root):
    DATABASE_URL=sqlite:///./mock_draft_demo.db .venv/Scripts/python.exe scripts/seed_demo_league.py
    .venv-api/Scripts/python.exe scripts/run_mock_league_home.py

Runs on a throwaway copy of the seeded DB (mock_run.db); FSC_GRANT_ALL_PREMIUM is
left unset so commissioner mode is really gated.
"""

import datetime
import os
import shutil
import sys
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

from fantasy_sports.api.main import app
from fantasy_sports.platform import entitlements
from fantasy_sports.platform.ai.auction_ai import STARTER_MIN
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models import core  # noqa: F401
from fantasy_sports.platform.db.models.core import (
    FreeAgencyTransaction,
    LineupEntry,
    RecruitingAward,
    RecruitingDraft,
    RosterEntry,
    Team,
    User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine as db_engine
from fantasy_sports.platform.recruiting import engine as recruiting


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    from fantasy_sports.platform.db.schema_sync import ensure_latest_schema
    ensure_latest_schema(db_engine)
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

    # ---- a league whose recruiting hasn't ended: free agency is closed --------------------
    early = client.post("/leagues/sandbox", json={"name": "Early"}, headers=B).json()
    e_teams = client.get(f"/leagues/{early['id']}/teams").json()
    fa0 = client.get(f"/leagues/{early['id']}/free-agents").json()
    check(fa0["open"] is False and fa0["players"] == [], "free agency must be closed until recruiting ends")
    r = client.post(f"/teams/{e_teams[0]['id']}/free-agency/add", json={"draftable_asset_id": 1}, headers=B)
    check(r.status_code == 400 and "opens when" in r.text, f"a signing before recruiting ends must be refused: {r.text}")
    ov0 = client.get(f"/leagues/{early['id']}/overview").json()
    check(ov0["free_agency_open"] is False and ov0["draft"]["status"] == "scheduled", f"overview before recruiting: {ov0['draft']}")
    print("Before recruiting ends: free agency is closed and a signing is refused")

    # ---- simulate: recruiting closes -> the AI's required fill happens by itself ----------------
    league = client.post("/leagues/sandbox", json={"name": "League Home Test"}, headers=B).json()
    lid = league["id"]
    teams = client.get(f"/leagues/{lid}/teams").json()
    check(client.post(f"/recruiting/league/{lid}/simulate", headers=B).status_code == 200, "simulate failed")
    db.expire_all()
    draft = db.query(RecruitingDraft).filter_by(league_id=lid).one()
    recruited = Counter(a.team_id for a in db.query(RecruitingAward).filter_by(draft_id=draft.id))
    check(min(recruited[t["id"]] for t in teams) < 20, "the test needs at least one team that recruited fewer than 20")
    team_ids = [t["id"] for t in teams]
    roster = Counter(r.team_id for r in db.query(RosterEntry).filter(RosterEntry.team_id.in_(team_ids)))
    check(all(roster[t] == 20 for t in team_ids), f"every AI team must end with a full roster: {sorted(roster.values())}")
    fa_adds = db.query(FreeAgencyTransaction).filter_by(league_id=lid, kind="add").all()
    signed = Counter(t.team_id for t in fa_adds)
    for t in team_ids:
        check(signed[t] == 20 - recruited[t], f"team {t}: signed {signed[t]}, expected {20 - recruited[t]}")
    check(all(t.reason == "ai_fill" and t.price >= 1 for t in fa_adds), "every fill signing is logged as ai_fill with a price")
    assets = [r.draftable_asset_id for r in db.query(RosterEntry).filter(RosterEntry.team_id.in_(team_ids))]
    check(len(assets) == len(set(assets)), "a player is on two rosters in the same league")
    check(all(r.acquired_via == "free_agency" for r in db.query(RosterEntry).filter(
        RosterEntry.team_id.in_(team_ids), RosterEntry.draftable_asset_id.in_([t.draftable_asset_id for t in fa_adds]))),
        "signed players should be marked acquired via free agency")
    # starting positions a team lacked were filled first: a team can only still lack one if the
    # pool has none left at that position (e.g. only 16 DL groups exist) or it never had a spot
    from fantasy_sports.api.helpers import draftable_asset_position
    from fantasy_sports.platform import free_agency
    left = Counter(draftable_asset_position(db.get(core.DraftableAsset, a)) for a in
                   set(free_agency.pool_ids(db, lid)) - free_agency.rostered_ids(db, lid))
    lacking = 0
    for t in team_ids:
        have = Counter(draftable_asset_position(r.draftable_asset) for r in db.query(RosterEntry).filter_by(team_id=t))
        for p, n in STARTER_MIN.items():
            if have[p] < n:
                lacking += 1
                check(left[p] == 0 or signed[t] == 0, f"team {t} lacks a {p} though {left[p]} free agents at {p} remain and it signed {signed[t]}")
    print(f"AI required fill: recruiting closed -> {len(fa_adds)} free agents signed; all 12 rosters are 20/20, "
          f"{lacking} missing starting slot(s) only where the pool ran out; recruited counts were {sorted(recruited[t] for t in team_ids)}")
    over = [t for t in team_ids if _fa_money(db, t) < 0]
    print(f"  ({len(over)} team(s) had to take the cheapest available because they couldn't afford better)")

    # ---- overview / free agents / transactions / stats --------------------------------------------
    ov = client.get(f"/leagues/{lid}/overview").json()
    check(ov["free_agency_open"] and ov["draft"]["status"] == "closed" and len(ov["teams"]) == 12, "overview wrong")
    check(all(t["roster_count"] == 20 and t["open_spots"] == 0 and t["is_ai_owned"] for t in ov["teams"]), "overview roster counts")
    check(ov["ai_teams_with_open_spots"] == 0 and ov["commissioner_name"] and ov["season_weeks"] == 10 and ov["playoff_weeks"] == 3, "overview fields")
    check(all(t["conference_name"] and t["weekly_budget"] in (100, 200, 300) for t in ov["teams"]), "team summary fields")
    fa = client.get(f"/leagues/{lid}/free-agents").json()
    check(fa["open"] and "provisional" in fa["price_note"].lower(), "the price note must say the rule is provisional")
    check(len(fa["players"]) == 719 - 240 and all(p["asking_price"] >= 1 for p in fa["players"]), f"free agent pool wrong: {len(fa['players'])}")
    check(not set(p["draftable_asset_id"] for p in fa["players"]) & set(assets), "a rostered player shows up as a free agent")
    log = client.get(f"/leagues/{lid}/transactions").json()
    kinds = Counter(x["kind"] for x in log)
    check(kinds["recruited"] == sum(recruited.values()) and kinds["added"] == len(fa_adds), f"log counts: {kinds}")
    check(all(x["price"] is None for x in log if x["kind"] == "recruited"), "recruiting bid amounts must never appear in the log")
    check(all(x["price"] is not None and x["reason"] == "ai_fill" for x in log if x["kind"] == "added"), "AI signings: price + reason")
    check([x["at"] for x in log] == sorted((x["at"] for x in log), reverse=True), "the log must be newest first")
    st = client.get(f"/leagues/{lid}/stats").json()
    owned = sum(1 for r in st if r["fantasy_team_id"] is not None)
    check(len(st) == 719 and owned == 240 and any(r["weekly"] for r in st) and any(r["last_season_points"] for r in st), "stats rows wrong")
    print(f"Endpoints: overview, {len(fa['players'])} free agents with asking prices, {len(log)}-entry transaction log "
          f"(newest first, no recruiting bid amounts), {len(st)} stat rows ({owned} rostered)")

    # ---- signing and releasing: acting for an AI team as commissioner --------------------------------
    t1 = team_ids[0]
    wk = client.get(f"/leagues/{lid}/weeks").json()[0]["id"]
    client.post(f"/teams/{t1}/lineup/auto", params={"week_id": wk}, headers=B)
    victim = next(e for e in client.get(f"/teams/{t1}/lineup", params={"week_id": wk}).json() if e["slot_code"] == "QB")["draftable_asset_id"]
    D = client.post(f"/teams/{t1}/free-agency/drop", json={"draftable_asset_id": victim}, headers=B)
    check(D.status_code == 200 and D.json()["kind"] == "dropped" and D.json()["reason"] == "commissioner", D.text)
    check(db.query(LineupEntry).filter_by(team_id=t1, draftable_asset_id=victim).count() == 0, "a dropped player must leave the lineup")
    check(victim in {p["draftable_asset_id"] for p in client.get(f"/leagues/{lid}/free-agents").json()["players"]}, "a dropped player becomes a free agent")
    s1 = client.get(f"/teams/{t1}/free-agency/summary", headers=B).json()
    check(s1["roster_count"] == 19 and s1["open_spots"] == 1 and s1["fa_money"] is not None, f"summary: {s1}")
    check(client.get(f"/teams/{t1}/free-agency/summary").json()["fa_money"] is None, "money is private to those who manage the team")
    check(client.get(f"/teams/{t1}/free-agency/summary", headers=O).json()["fa_money"] is None, "another user must not see the team's money")
    cheap = min(client.get(f"/leagues/{lid}/free-agents").json()["players"], key=lambda p: p["asking_price"])
    A = client.post(f"/teams/{t1}/free-agency/add", json={"draftable_asset_id": cheap["draftable_asset_id"]}, headers=B)
    check(A.status_code == 200 and A.json()["kind"] == "added" and A.json()["price"] == cheap["asking_price"], A.text)
    r = client.post(f"/teams/{t1}/free-agency/add", json={"draftable_asset_id": cheap["draftable_asset_id"]}, headers=B)
    check(r.status_code == 400 and "already" in r.text, f"signing a rostered player: {r.text}")
    other_fa = next(p for p in client.get(f"/leagues/{lid}/free-agents").json()["players"])
    r = client.post(f"/teams/{t1}/free-agency/add", json={"draftable_asset_id": other_fa["draftable_asset_id"]}, headers=B)
    check(r.status_code == 400 and "full" in r.text, f"signing with a full roster: {r.text}")
    check(client.post(f"/teams/{t1}/free-agency/drop", json={"draftable_asset_id": 999999}, headers=B).status_code == 400, "dropping a stranger")
    print("Signing/releasing: a commissioner-mode commissioner dropped a starter (gone from the lineup, back in the pool) "
          "and signed a replacement; a rostered player, a full roster and a non-roster drop were each refused")

    # ---- money: a signing the team can't afford is refused -----------------------------------------------
    team_obj = db.get(Team, t1)
    db.add(FreeAgencyTransaction(league_id=lid, team_id=t1, draftable_asset_id=victim, kind="add", price=10**6, reason="user",
                                 created_at=datetime.datetime.utcnow()))
    db.commit()
    victim2 = client.get(f"/teams/{t1}/lineup", params={"week_id": wk}).json()[0]["draftable_asset_id"]
    client.post(f"/teams/{t1}/free-agency/drop", json={"draftable_asset_id": victim2}, headers=B)
    r = client.post(f"/teams/{t1}/free-agency/add", json={"draftable_asset_id": victim2}, headers=B)
    check(r.status_code == 400 and "free-agency money" in r.text, f"an unaffordable signing must be refused: {r.text}")
    db.query(FreeAgencyTransaction).filter(FreeAgencyTransaction.price == 10**6).delete()
    db.commit()
    print("Money: a signing beyond the team's free-agency money is refused with the amounts")

    # ---- the AI fill is idempotent, and re-fills spots opened later ---------------------------------------
    check(client.post(f"/leagues/{lid}/free-agency/ai-fill", headers=O).status_code == 403, "only the commissioner")
    for e in db.query(RosterEntry).filter_by(team_id=team_ids[1]).limit(3).all():
        db.delete(e)
    db.commit()
    r = client.post(f"/leagues/{lid}/free-agency/ai-fill", headers=B).json()
    got = {x["team_id"]: x["players"] for x in r["signed"]}
    # 3 spots opened just above, plus the 1 spot left on the team whose starter was dropped in the money test
    check(r["total"] == 4 and got == {team_ids[1]: 3, t1: 1}, f"the fill should re-fill exactly the 4 open spots: {r}")
    check(client.post(f"/leagues/{lid}/free-agency/ai-fill", headers=B).json()["total"] == 0, "the fill must be idempotent")
    print("AI fill endpoint: refilled the 4 open spots, second run signed nobody")

    # ---- permissions ---------------------------------------------------------------------------------------
    body = {"draftable_asset_id": other_fa["draftable_asset_id"]}
    check(client.post(f"/teams/{t1}/free-agency/add", json=body).status_code == 401, "no token")
    check(client.post(f"/teams/{t1}/free-agency/add", json=body, headers=O).status_code == 403, "a stranger can't sign for an AI team")
    check(client.post(f"/teams/{t1}/free-agency/drop", json={"draftable_asset_id": cheap["draftable_asset_id"]}, headers=O).status_code == 403, "nor drop")
    print("Permissions: no token -> 401; another user -> 403 on add and drop")

    # ---- roster cost includes free-agent signings ---------------------------------------------------------
    db.expire_all()
    draft = db.query(RecruitingDraft).filter_by(league_id=lid).one()
    fin = recruiting.team_finances(db, draft)
    for t in team_ids:
        won = sum(c.amount for c in db.query(core.RecruitingCharge).filter_by(draft_id=draft.id, team_id=t, is_winner=True))
        fa_cost = sum(x.price for x in db.query(FreeAgencyTransaction).filter_by(team_id=t, kind="add"))
        check(fin[t]["roster_cost"] == won + fa_cost, f"team {t}: roster cost {fin[t]['roster_cost']} != recruited {won} + free agents {fa_cost}")
    print("Roster Cost = what was paid to recruit the roster + what free-agent signings cost")
    print("MOCK LEAGUE HOME PASSED")


def _fa_money(db, team_id):
    from fantasy_sports.platform import free_agency
    return free_agency.team_money(db, db.get(Team, team_id))["fa_money"]


if __name__ == "__main__":
    main()
