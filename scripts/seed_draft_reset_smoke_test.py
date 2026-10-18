"""
Draft reset (platform/draft_reset.py), through the real HTTP API (FastAPI TestClient) on a throwaway SQLite file
(draft_reset_smoke_test.db):

  * snake (NHL): the commissioner resets a running draft alone to a chosen pick and to pick 1; rosters, hockey line order and
    picks go back; non-commissioners are refused; bad `keep` values are refused; a timed draft comes back paused on a full clock
  * snake, completed: unanimous vote (one rejection ends it, every approval applies it), the 'commissioner' and 'off' policies,
    activity since the draft (a free-agency move) blocks it, one open vote at a time, a vote refused at the end is 'failed'
  * auction (college): reset to a chosen round and to the start refunds budgets, drops rosters and hands nomination back
  * recruiting (college): a closed draft reset by vote reopens on the chosen period, undoes later awards and charges

    .venv-api/Scripts/python.exe scripts/seed_draft_reset_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("DRAFT_RESET_SMOKE_DB", "draft_reset_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import draft_reset
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    AuctionBid, AuctionDraft, AuctionRound, AuctionTeamBudget, CommissionerAction, Draft, DraftPick, DraftableAsset, EmailOutbox,
    FreeAgencyTransaction, League, Player, RealTeam, RecruitingAward, RecruitingBid, RecruitingCharge, RecruitingDraft,
    RosterEntry, Team, TeamPeckingOrder, Universe, User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.platform.recruiting import engine as recruiting
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()
checks = 0


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


# ---- reference data: an NHL pool and a small college pool ---------------------------------------------------------
nhl, _ = seed_hockey_reference_data(db)
real = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
db.add(real)
db.flush()
skaters = [Player(universe_id=nhl.id, real_team_id=real.id, full_name=f"Skater {i:03d}", position="C") for i in range(80)]
db.add_all(skaters)
db.flush()
db.add_all([DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id) for p in skaters])

ncaaf = Universe(sport="football", level="college", code="ncaaf", display_name="NCAAF")
db.add(ncaaf)
db.flush()
georgia = RealTeam(universe_id=ncaaf.id, name="Georgia", conference="SEC", classification="fbs")
db.add(georgia)
db.flush()
college = [Player(universe_id=ncaaf.id, real_team_id=georgia.id, full_name=f"College {i:02d}", position="RB") for i in range(12)]
db.add_all(college)
db.flush()
college_assets = [DraftableAsset(universe_id=ncaaf.id, asset_type="player", player_id=p.id) for p in college]
db.add_all(college_assets)
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "a", "b", "c", "d", "stranger")}
db.add_all(users.values())
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


HDR = {n: login(u) for n, u in users.items()}


def make_league(name, code, size, rounds, owners):
    resp = client.post("/leagues", json={"name": name, "commissioner_user_id": users["commish"].id, "universe_codes": [code],
                                         "size": size, "draft_rounds": rounds}, headers=HDR["commish"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["draft_reset_policy"] == "unanimous", "unanimous is the default"
    lid = resp.json()["id"]
    teams = [Team(league_id=lid, name=f"{name} {n}", owner_user_id=users[n].id) for n in owners]
    db.add_all(teams)
    db.commit()
    return lid, [t.id for t in teams]


def policy(lid, value):
    resp = client.patch(f"/leagues/{lid}/settings", json={"draft_reset_policy": value}, headers=HDR["commish"])
    assert resp.status_code == 200, resp.text
    check(resp.json()["draft_reset_policy"] == value, "policy saved")


def reset(lid, kind, draft_id, keep=0, who="commish"):
    return client.post(f"/leagues/{lid}/draft-resets", json={"kind": kind, "draft_id": draft_id, "keep": keep}, headers=HDR[who])


def state(lid, who="commish"):
    return client.get(f"/leagues/{lid}/draft-resets", headers=HDR[who]).json()


def roster_ids(tids):
    db.expire_all()
    return sorted(r.draftable_asset_id for r in db.query(RosterEntry).filter(RosterEntry.team_id.in_(tids)))


def picks_made(draft_id):
    db.expire_all()
    return db.query(DraftPick).filter(DraftPick.draft_id == draft_id, DraftPick.draftable_asset_id.isnot(None)).count()


def finish_snake(draft_id):
    for _ in range(400):
        if client.post(f"/drafts/{draft_id}/autopick").status_code != 200:
            break
    db.expire_all()
    return db.get(Draft, draft_id).status


TOTAL = 72      # 4 teams x 18 rounds (an NHL roster)


# ==== 1. snake, running ====================================================================================================
lid, tids = make_league("Snake", "nhl", 4, 18, ("a", "b", "c", "d"))
draft_id = client.post("/drafts/start", json={"league_id": lid}, headers=HDR["commish"]).json()["id"]
for _ in range(5):
    check(client.post(f"/drafts/{draft_id}/autopick").status_code == 200, "autopick")
check(picks_made(draft_id) == 5 and len(roster_ids(tids)) == 5, "five picks made")

st = state(lid)
check(st["policy"] == "unanimous" and st["drafts"][0]["done"] == 5 and st["drafts"][0]["mode"] == "now"
      and st["drafts"][0]["can_reset"], f"a running draft resets by the commissioner alone: {st}")
check(client.get(f"/leagues/{lid}/draft-resets", headers=HDR["stranger"]).status_code == 403, "outsiders can't look")
check(reset(lid, "snake", draft_id, 2, who="a").status_code == 403, "a manager can't reset")
check(reset(lid, "snake", draft_id, 5).status_code == 409, "keep must be below what is done")
check(reset(lid, "snake", draft_id, 9).status_code == 409, "keep can't pass what is done")
check(reset(lid, "snake", 9999).status_code == 409, "unknown draft")
check(picks_made(draft_id) == 5, "refused resets change nothing")

resp = reset(lid, "snake", draft_id, 2)
check(resp.status_code == 200 and resp.json()["status"] == "applied", resp.text)
check(picks_made(draft_id) == 2 and len(roster_ids(tids)) == 2, "back to two picks and two rostered players")
kept = {p.draftable_asset_id for p in db.query(DraftPick).filter(DraftPick.draft_id == draft_id, DraftPick.pick_number <= 2)}
check(set(roster_ids(tids)) == kept, "the kept picks stay rostered")
ranked = {r.draftable_asset_id for r in db.query(TeamPeckingOrder).filter(TeamPeckingOrder.team_id.in_(tids))}
check(ranked <= kept, "hockey line order dropped the undone players")
check(db.query(CommissionerAction).filter_by(league_id=lid, action="draft_reset").count() == 1, "the reset is in the action log")
check(db.query(EmailOutbox).filter_by(league_id=lid, kind="draft_reset").count() == 5, "managers and commissioner are told")
check(db.get(Draft, draft_id).status == "in_progress", "still in progress")
check(client.post(f"/drafts/{draft_id}/autopick").status_code == 200 and picks_made(draft_id) == 3, "the draft carries on")

check(reset(lid, "snake", draft_id, 0).json()["status"] == "applied", "reset to pick 1")
check(picks_made(draft_id) == 0 and roster_ids(tids) == [], "nothing drafted, nothing rostered")
check(reset(lid, "snake", draft_id, 0).status_code == 409, "nothing left to reset")

# ==== 2. snake, completed: the vote ========================================================================================
check(finish_snake(draft_id) == "complete", "draft completes")
check(len(roster_ids(tids)) == TOTAL, "every pick rostered")
st = state(lid)["drafts"][0]
check(st["mode"] == "vote" and st["can_reset"], f"a completed draft goes to a vote: {st}")

resp = reset(lid, "snake", draft_id, 0)
req = resp.json()
check(resp.status_code == 200 and req["status"] == "voting" and len(req["votes"]) == 4, resp.text)
check(picks_made(draft_id) == TOTAL, "nothing happens before the votes are in")
check(reset(lid, "snake", draft_id, 4).status_code == 409, "one open vote at a time")
vote = lambda r, who, v="approve": client.post(f"/draft-resets/{r}/vote", json={"vote": v}, headers=HDR[who])
check(vote(req["id"], "stranger").status_code == 403, "outsiders can't vote")
check(vote(req["id"], "commish").status_code == 403, "the commissioner doesn't vote unless they manage a team")
check(vote(req["id"], "a").json()["my_vote"] == "approve", "a approves")
check(vote(req["id"], "b").json()["status"] == "voting", "still waiting")
resp = vote(req["id"], "c", "reject")
check(resp.json()["status"] == "rejected", "one rejection ends it")
check(picks_made(draft_id) == TOTAL, "a rejected reset changes nothing")
check(vote(req["id"], "d").status_code == 409, "a closed vote takes no more votes")

req = reset(lid, "snake", draft_id, 6).json()
for who in ("a", "b", "c"):
    check(vote(req["id"], who).json()["status"] == "voting", f"{who} approved, d still to go")
resp = vote(req["id"], "d")
check(resp.json()["status"] == "applied", resp.text)
check(picks_made(draft_id) == 6 and len(roster_ids(tids)) == 6, "every approval applies it, back to six picks")
check(db.get(Draft, draft_id).status == "in_progress" and db.get(Draft, draft_id).completed_at is None, "draft reopened")
check(len(state(lid)["history"]) == 4, "every request is in the history")

# withdrawn
check(finish_snake(draft_id) == "complete", "draft completes again")
req = reset(lid, "snake", draft_id, 0).json()
check(client.post(f"/draft-resets/{req['id']}/cancel", headers=HDR["a"]).status_code == 403, "only the commissioner withdraws")
check(client.post(f"/draft-resets/{req['id']}/cancel", headers=HDR["commish"]).json()["status"] == "cancelled", "withdrawn")

# built on since the draft: refused at the start, and failed if it happens while the vote is open
req = reset(lid, "snake", draft_id, 0).json()
for who in ("a", "b", "c"):
    vote(req["id"], who)
db.add(FreeAgencyTransaction(league_id=lid, team_id=tids[0], draftable_asset_id=db.query(DraftableAsset).filter_by(universe_id=nhl.id).first().id,
                             kind="add", price=0, reason="user", created_at=datetime.datetime.utcnow()))
db.commit()
resp = vote(req["id"], "d")
check(resp.json()["status"] == "failed" and "free-agency" in (resp.json()["failure_reason"] or ""),
      f"a vote that ends after activity is failed: {resp.text}")
check(picks_made(draft_id) == TOTAL, "a failed reset changes nothing")
check(reset(lid, "snake", draft_id, 0).status_code == 409, "activity since the draft blocks a new request")
check(state(lid)["drafts"][0]["can_reset"] is False, "and the state says why")
db.query(FreeAgencyTransaction).filter_by(league_id=lid).delete()
db.commit()

# the other two policies
policy(lid, "commissioner")
check(state(lid)["drafts"][0]["mode"] == "now", "commissioner policy: now")
check(reset(lid, "snake", draft_id, 8).json()["status"] == "applied" and picks_made(draft_id) == 8,
      "the commissioner alone resets a completed draft")
check(finish_snake(draft_id) == "complete", "complete again")
policy(lid, "off")
resp = reset(lid, "snake", draft_id, 0)
check(resp.status_code == 409 and picks_made(draft_id) == TOTAL, "policy off refuses")
check(reset(lid, "snake", draft_id, 3, who="a").status_code == 403, "still only the commissioner asks")
policy(lid, "unanimous")

# ==== 3. snake, timed =======================================================================================================
lid2, tids2 = make_league("Timed", "nhl", 4, 18, ("a", "b", "c", "d"))
did2 = client.post("/drafts/start", json={"league_id": lid2, "pick_seconds": 60}, headers=HDR["commish"]).json()["id"]
d2 = db.get(Draft, did2)
back = datetime.datetime.utcnow() - datetime.timedelta(seconds=150)
d2.pick_started_at, d2.deadline_at = back, back + datetime.timedelta(seconds=60)
db.commit()
client.post(f"/drafts/{did2}/process-overdue")
check(picks_made(did2) >= 2, "overdue picks were made")
check(reset(lid2, "snake", did2, 1).json()["status"] == "applied", "reset a timed draft")
clock = client.get(f"/drafts/{did2}/clock").json()
check(clock["paused"] is True and clock["remaining_seconds"] == 60 and picks_made(did2) == 1,
      f"a timed draft comes back paused on a full clock: {clock}")
check(client.post(f"/drafts/{did2}/resume", headers=HDR["commish"]).json()["paused"] is False, "and resumes")

# ==== 4. auction ============================================================================================================
from fantasy_sports.platform.auction import engine as auction_engine

lid3, tids3 = make_league("Auction", "ncaaf", 3, 3, ("a", "b", "c"))
aid = client.post("/auctions/start", json={"league_id": lid3, "round_seconds": 30}, headers=HDR["commish"]).json()["id"]
order = db.get(AuctionDraft, aid).nomination_order
owner = dict(zip(tids3, ("a", "b", "c")))
won = []
for n in range(3):
    turn = client.get(f"/auctions/{aid}").json()["next_nominator_index"]
    nominator = order[turn]
    rnd = client.post(f"/auctions/{aid}/nominate", json={"draftable_asset_id": college_assets[n].id, "nominated_by_team_id": nominator},
                      headers=HDR[owner[nominator]]).json()
    check("id" in rnd, rnd)
    bidder = tids3[n % 3]
    check(client.post(f"/auctions/{aid}/rounds/{rnd['id']}/bids", json={"team_id": bidder, "amount": 10 + n},
                      headers=HDR[owner[bidder]]).status_code == 200, "bid")
    auction_engine.resolve_round(db, rnd["id"], force=True)
    db.commit()
    won.append((rnd["id"], bidder, 10 + n, nominator))
db.expire_all()
start = {b.team_id: b.starting_budget for b in db.query(AuctionTeamBudget).filter_by(auction_draft_id=aid)}
check(len(roster_ids(tids3)) == 3, "three players sold")
check(state(lid3)["drafts"][0]["done"] == 3, "three rounds held")

check(reset(lid3, "auction", aid, 1).json()["status"] == "applied", "auction reset to after round 1")
db.expire_all()
rounds = db.query(AuctionRound).filter_by(auction_draft_id=aid).all()
check([r.round_number for r in rounds] == [1], "later rounds are gone")
check(db.query(AuctionBid).filter(AuctionBid.auction_round_id.in_([w[0] for w in won[1:]])).count() == 0, "and their bids")
check(roster_ids(tids3) == [college_assets[0].id], "only the first sale is rostered")
for b in db.query(AuctionTeamBudget).filter_by(auction_draft_id=aid):
    spent = 10 if b.team_id == won[0][1] else 0
    check(b.remaining_budget == start[b.team_id] - spent and b.assets_won == (1 if spent else 0), f"budget refunded for team {b.team_id}")
auction = db.get(AuctionDraft, aid)
check(order[auction.next_nominator_index] == won[1][3], "the team that nominated round 2 is on turn again")
check(reset(lid3, "auction", aid, 0).json()["status"] == "applied", "auction reset to the start")
db.expire_all()
check(db.query(AuctionRound).filter_by(auction_draft_id=aid).count() == 0 and roster_ids(tids3) == [], "empty again")
check(all(b.remaining_budget == b.starting_budget and b.assets_won == 0 for b in db.query(AuctionTeamBudget).filter_by(auction_draft_id=aid)),
      "every budget back at the start")

# ==== 5. recruiting =========================================================================================================
lid4, tids4 = make_league("Recruit", "ncaaf", 3, 2, ("a", "b", "c"))
db.get(League, lid4).draft_type = "recruit"
db.commit()
t0 = datetime.datetime.utcnow()
rd = recruiting.open_draft(db, lid4, t0)
rd.period_seconds, rd.buffer_seconds = 60, 20
db.commit()
did4 = rd.id
for i, tid in enumerate(tids4):
    recruiting.place_bid(db, did4, tid, college_assets[i].id, 1, t0)
db.commit()
recruiting.process_period_now(db, did4, t0 + datetime.timedelta(seconds=10))     # period 1: prices unmet, bids carry
db.commit()
later = recruiting.period_start(db.get(RecruitingDraft, did4), 2) + datetime.timedelta(seconds=1)
recruiting.process_period_now(db, did4, later)                                    # period 2 is the last: highest bidders get them
db.commit()
db.expire_all()
rd = db.get(RecruitingDraft, did4)
check(rd.status == "closed" and rd.periods_processed == 2, "recruiting closed after its last period")
check(db.query(RecruitingAward).filter_by(draft_id=did4).count() == 3 and len(roster_ids(tids4)) == 3, "three players recruited")
awards_p1 = db.query(RecruitingAward).filter_by(draft_id=did4, period_number=1).count()

st = state(lid4)["drafts"][0]
check(st["kind"] == "recruiting" and st["mode"] == "vote" and st["done"] == 2, f"closed recruiting goes to a vote: {st}")
req = reset(lid4, "recruiting", did4, 1).json()
check(req["status"] == "voting", req)
for who in ("a", "b"):
    check(vote(req["id"], who).json()["status"] == "voting", "waiting")
check(vote(req["id"], "c").json()["status"] == "applied", "all approved")
db.expire_all()
rd = db.get(RecruitingDraft, did4)
check(rd.status == "open" and rd.ended_at is None and rd.periods_processed == 1 and rd.total_periods == 2, "reopened on period 2")
check(recruiting.current_period(rd) == 2 and not recruiting.in_buffer(rd, datetime.datetime.utcnow()), "period 2 is open for bids now")
check(db.query(RecruitingAward).filter_by(draft_id=did4).count() == awards_p1, "awards from the undone period are gone")
check(len(roster_ids(tids4)) == awards_p1, "and their roster entries")
check(db.query(RecruitingCharge).filter(RecruitingCharge.draft_id == did4, RecruitingCharge.period_number > 1).count() == 0, "and the charges")
check(db.query(RecruitingBid).filter_by(draft_id=did4).count() == 3, "standing bids stay on a partial reset")

policy(lid4, "commissioner")
check(reset(lid4, "recruiting", did4, 0).json()["status"] == "applied", "reset recruiting from the start")
db.expire_all()
rd = db.get(RecruitingDraft, did4)
check(rd.periods_processed == 0 and db.query(RecruitingBid).filter_by(draft_id=did4).count() == 0
      and db.query(RecruitingAward).filter_by(draft_id=did4).count() == 0 and roster_ids(tids4) == [], "a full reset clears bids too")
check(recruiting.current_period(rd) == 1 and not recruiting.in_buffer(rd, datetime.datetime.utcnow()), "period 1 is open")

# ==== 6. the setting ========================================================================================================
check(client.patch(f"/leagues/{lid}/settings", json={"draft_reset_policy": "sometimes"}, headers=HDR["commish"]).status_code == 422,
      "unknown policy refused")
check(client.patch(f"/leagues/{lid}/settings", json={"draft_reset_policy": "off"}, headers=HDR["a"]).status_code == 403,
      "only the commissioner changes it")
resp = client.post("/leagues", json={"name": "Wizard", "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"],
                                     "size": 4, "draft_reset_policy": "commissioner"}, headers=HDR["commish"])
check(resp.status_code == 200 and resp.json()["draft_reset_policy"] == "commissioner", "the wizard can set it at creation")

print(f"Draft reset smoke test passed ({checks} checks).")
