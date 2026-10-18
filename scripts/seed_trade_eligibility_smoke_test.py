"""
CFG-06: paid and free league trade eligibility, through the real HTTP API (FastAPI TestClient) on a throwaway SQLite file
(trade_eligibility_smoke_test.db): league type, dues, eliminated teams, the free-league majority + commissioner approval,
the deadline, ownership, double execution, rechecks at execution and franchise continuity.

    .venv-api/Scripts/python.exe scripts/seed_trade_eligibility_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("TRADE_ELIG_SMOKE_DB", "trade_eligibility_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import elimination, trade_eligibility
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, League, Player, RealTeam, RosterEntry, SeasonDues, Team, Trade, User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()

nhl, _ = seed_hockey_reference_data(db)
real = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
db.add(real)
db.flush()
players = [Player(universe_id=nhl.id, real_team_id=real.id, full_name=f"Skater {i:03d}", position="C") for i in range(400)]
db.add_all(players)
db.flush()
db.add_all([DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id) for p in players])
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "a", "b", "c", "d", "stranger")}
db.add_all(users.values())
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


HDR = {n: login(u) for n, u in users.items()}
DUES = 5000
checks = 0


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


def make_league(name, league_type="FREE", league_format="dynasty"):
    body = {"name": name, "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"], "size": 4,
            "league_type": league_type}
    if league_type == "PAID":
        body["annual_dues_cents"] = DUES
    resp = client.post("/leagues", json=body, headers=HDR["commish"])
    assert resp.status_code == 200, resp.text
    lid = resp.json()["id"]
    # FS-036: no review window here, so accepted trades execute at once as these CFG-06 checks expect (trade_review has its own test).
    assert client.patch(f"/leagues/{lid}/settings", json={"league_format": league_format, "trade_review_hours": 0},
                        headers=HDR["commish"]).status_code == 200
    teams = [Team(league_id=lid, name=f"{name} {n}", owner_user_id=users[n].id) for n in ("a", "b", "c", "d")]
    db.add_all(teams)
    db.commit()
    resp = client.post("/drafts/start", json={"league_id": lid}, headers=HDR["commish"])
    assert resp.status_code == 200, resp.text
    draft_id = resp.json()["id"]
    for _ in range(600):
        if client.post(f"/drafts/{draft_id}/autopick").status_code != 200:
            break
    return lid, [t.id for t in teams]


def roster(tid):
    return sorted(r.draftable_asset_id for r in db.query(RosterEntry).filter_by(team_id=tid))


def owner_of(aid, lid):
    row = (db.query(RosterEntry).join(Team, Team.id == RosterEntry.team_id)
           .filter(Team.league_id == lid, RosterEntry.draftable_asset_id == aid).first())
    return row.team_id if row else None


def propose(lid, me, them, who, send=(), receive=(), send_picks=(), receive_picks=()):
    return client.post(f"/leagues/{lid}/trades", headers=HDR[who], json={
        "proposer_team_id": me, "counterparty_team_id": them, "send_asset_ids": list(send), "receive_asset_ids": list(receive),
        "send_picks": [{"season": s, "round": r, "original_team_id": o} for s, r, o in send_picks],
        "receive_picks": [{"season": s, "round": r, "original_team_id": o} for s, r, o in receive_picks]})


def respond(tid, team, who, accept=True):
    return client.post(f"/trades/{tid}/respond", json={"team_id": team, "accept": accept}, headers=HDR[who])


def vote(tid, team, who, v="approve"):
    return client.post(f"/trades/{tid}/vote", json={"team_id": team, "vote": v}, headers=HDR[who])


def decide(tid, who, d="approve"):
    return client.post(f"/trades/{tid}/commissioner-decision", json={"decision": d}, headers=HDR[who])


def pay(lid, tid, season, status="paid", ref=None, who="commish", amount=DUES):
    ref = ref or f"ref-{lid}-{tid}-{season}-{status}"
    return client.post(f"/leagues/{lid}/teams/{tid}/dues",
                       json={"season_number": season, "amount_cents": amount, "status": status, "payment_reference": ref},
                       headers=HDR[who])


def eliminate(lid, tid):
    league = db.get(League, lid)
    elimination.mark_eliminated(db, league, db.get(Team, tid))
    db.commit()


def status_of(tid):
    db.expire_all()
    return db.get(Trade, tid).status


# ---- League classification ------------------------------------------------------------------------------------------
resp = client.post("/leagues", json={"name": "Bad", "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"],
                                     "size": 4, "league_type": "PAID"}, headers=HDR["commish"])
check(resp.status_code == 400, "a paid league needs a dues amount")
lid, (a, b, c, d) = make_league("Free")
check(client.get(f"/leagues/{lid}").json()["league_type"] == "FREE", "default type is FREE")
check(client.patch(f"/leagues/{lid}/settings", json={"league_type": "PAID"}, headers=HDR["a"]).status_code == 403, "only the commissioner")
check(client.patch(f"/leagues/{lid}/settings", json={"league_type": "PAID"}, headers=HDR["commish"]).status_code == 400, "needs dues")
# Rosters exist, so this is midseason: the commissioner's change opens a vote and only applies when every manager approves.
resp = client.patch(f"/leagues/{lid}/settings", json={"league_type": "PAID", "annual_dues_cents": DUES}, headers=HDR["commish"])
check(resp.status_code == 200 and resp.json()["league_type"] == "FREE" and resp.headers.get("X-Setting-Changes-Opened"), resp.text)
cid = int(resp.headers["X-Setting-Changes-Opened"])
check(client.patch(f"/leagues/{lid}/settings", json={"league_type": "PAID"}, headers=HDR["commish"]).status_code == 409,
      "only one open vote per setting")
check(client.post(f"/leagues/{lid}/setting-changes/{cid}/vote", json={"vote": "approve"}, headers=HDR["stranger"]).status_code == 403,
      "only a manager in the league votes")
for who in ("a", "b", "c"):
    resp = client.post(f"/leagues/{lid}/setting-changes/{cid}/vote", json={"vote": "approve"}, headers=HDR[who])
    check(resp.status_code == 200 and resp.json()["status"] == "voting", "three of four approving is not enough: " + resp.text)
check(client.get(f"/leagues/{lid}").json()["league_type"] == "FREE", "still FREE")
resp = client.post(f"/leagues/{lid}/setting-changes/{cid}/vote", json={"vote": "approve"}, headers=HDR["d"])
check(resp.status_code == 200 and resp.json()["status"] == "applied", resp.text)
check(client.get(f"/leagues/{lid}").json()["league_type"] == "PAID", "every manager approved: applied")
check(client.post(f"/leagues/{lid}/setting-changes/{cid}/vote", json={"vote": "reject"}, headers=HDR["d"]).status_code == 409, "closed")
check(pay(lid, a, 2).status_code == 200, "commissioner records dues")
resp = client.patch(f"/leagues/{lid}/settings", json={"league_type": "FREE"}, headers=HDR["commish"])
check(resp.status_code == 200 and resp.json()["league_type"] == "PAID", "back to FREE is a vote too")
cid = int(resp.headers["X-Setting-Changes-Opened"])
resp = client.post(f"/leagues/{lid}/setting-changes/{cid}/vote", json={"vote": "reject"}, headers=HDR["b"])
check(resp.json()["status"] == "rejected" and client.get(f"/leagues/{lid}").json()["league_type"] == "PAID", "one rejection ends it")
check(db.query(SeasonDues).filter_by(league_id=lid).count() == 1, "the obligation is untouched")
# league_format is voted on the same way (redraft -> dynasty strands a roster of veterans).
resp = client.patch(f"/leagues/{lid}/settings", json={"league_format": "traditional_redraft"}, headers=HDR["commish"])
fid = int(resp.headers["X-Setting-Changes-Opened"])
check(resp.json()["league_format"] == "dynasty", "format unchanged until the vote passes")
check(client.post(f"/leagues/{lid}/setting-changes/{fid}/cancel", headers=HDR["a"]).status_code == 403, "only the commissioner withdraws")
check(client.post(f"/leagues/{lid}/setting-changes/{fid}/cancel", headers=HDR["commish"]).json()["status"] == "cancelled", "")
rows = client.get(f"/leagues/{lid}/setting-changes", headers=HDR["a"]).json()
check([r["status"] for r in rows] == ["cancelled", "rejected", "applied"] and len(rows[0]["votes"]) == 4, rows)
# Offseason or before the first draft: no vote.
lg = db.get(League, lid)
lg.offseason = True
db.commit()
check(client.patch(f"/leagues/{lid}/settings", json={"league_type": "FREE"}, headers=HDR["commish"]).json()["league_type"] == "FREE",
      "offseason changes apply at once")

# ---- FREE league: active teams ---------------------------------------------------------------------------------------
lid, (a, b, c, d) = make_league("FreeB")
resp = propose(lid, a, b, "a", send=[roster(a)[0]], receive=[roster(b)[0]])
check(resp.status_code == 200 and resp.json()["status"] == "proposed", resp.text)
check(resp.json()["eligibility"]["status"] == "ELIGIBLE_FOR_REVIEW", resp.json()["eligibility"])
check(respond(resp.json()["id"], b, "b").json()["status"] == "accepted", "active free player trade executes")
resp = propose(lid, a, b, "a", send_picks=[(2, 1, a)], receive_picks=[(2, 2, b)])       # FS-036: was a 200 ("free league picks need no dues")
check(resp.status_code == 400 and "Free leagues can't trade picks" in resp.json()["detail"], "free leagues can't trade picks: " + resp.text)
preview = client.post(f"/leagues/{lid}/trades/preview", headers=HDR["a"], json={
    "proposer_team_id": a, "counterparty_team_id": b, "send_picks": [{"season": 2, "round": 1, "original_team_id": a}],
    "receive_picks": [{"season": 2, "round": 2, "original_team_id": b}]}).json()
check(preview["status"] == "BLOCKED" and "FREE_LEAGUE_PICK_TRADING_LOCKED" in preview["blocking_codes"], preview)

# ---- PAID league: active teams ---------------------------------------------------------------------------------------
lid, (a, b, c, d) = make_league("Paid", "PAID")
resp = propose(lid, a, b, "a", send=[roster(a)[0]], receive=[roster(b)[0]])
check(resp.status_code == 200, "active paid player trade needs no dues")
client.post(f"/trades/{resp.json()['id']}/cancel", headers=HDR["a"])
resp = propose(lid, a, b, "a", send_picks=[(2, 1, a)], receive_picks=[(2, 2, b)])
check(resp.status_code == 400 and "2" in resp.json()["detail"] and "dues" in resp.json()["detail"], resp.text)
preview = client.post(f"/leagues/{lid}/trades/preview", headers=HDR["a"], json={
    "proposer_team_id": a, "counterparty_team_id": b, "send_picks": [{"season": 2, "round": 1, "original_team_id": a}],
    "receive_picks": [{"season": 2, "round": 2, "original_team_id": b}]}).json()
check(preview["status"] == "AWAITING_PAYMENT" and {p["team_id"] for p in preview["payment_requirements"]} == {a, b},
      "the manager sees what's missing before submitting: " + str(preview))
check(pay(lid, a, 2, "pending").status_code == 200 and pay(lid, a, 2, "failed", "f1").status_code == 200, "non-paid records")
check(pay(lid, a, 2, "refunded", "r1").status_code == 200 and pay(lid, a, 2, "disputed", "d1").status_code == 200, "non-paid records")
check(propose(lid, a, b, "a", send_picks=[(2, 1, a)], receive_picks=[(2, 2, b)]).status_code == 400,
      "pending, failed, refunded and disputed payments don't establish eligibility")
check(pay(lid, a, 2, who="a").status_code == 403, "managers can't write payment records")
check(pay(lid, a, 2, who="stranger").status_code == 403, "strangers can't either")
first = pay(lid, a, 2)
check(first.status_code == 200 and first.json()["status"] == "paid", first.text)
check(pay(lid, a, 2).json()["id"] == first.json()["id"], "repeating a reference is idempotent")
check(pay(lid, a, 2, ref="second-try").status_code == 400, "an already-secured season isn't collected twice")
check(propose(lid, a, b, "a", send_picks=[(2, 1, a)], receive_picks=[(2, 2, b)]).status_code == 400, "b still owes 2 for its pick")
check(pay(lid, b, 2).status_code == 200, "")
resp = propose(lid, a, b, "a", send_picks=[(2, 1, a)], receive_picks=[(2, 2, b)])
check(resp.status_code == 200 and resp.json()["status"] == "proposed", resp.text)
tid = resp.json()["id"]
check(client.get(f"/leagues/{lid}/trades/{tid}/eligibility", headers=HDR["stranger"]).status_code == 403, "outsiders can't read it")
# dues refunded before the other side accepts: the recheck at execution refuses it
rows = client.get(f"/leagues/{lid}/dues", headers=HDR["commish"]).json()
paid_a = next(r for r in rows if r["team_id"] == a and r["status"] == "paid")
check(client.post(f"/leagues/{lid}/dues/{paid_a['id']}/status", json={"status": "refunded"}, headers=HDR["commish"]).status_code == 200, "")
resp = respond(tid, b, "b")
check(resp.status_code == 400 and status_of(tid) == "proposed", "refunded dues stop execution: " + resp.text)
check(pay(lid, a, 2, ref="again").status_code == 200, "after a refund the season can be paid again")
check(respond(tid, b, "b").json()["status"] == "accepted", "executes once dues stand again")
check(respond(tid, b, "b").status_code == 400, "a trade can't execute twice")

# ---- Eliminated, PAID ------------------------------------------------------------------------------------------------
lid, (a, b, c, d) = make_league("PaidElim", "PAID")
eliminate(lid, a)
ra, rb = roster(a), roster(b)
check(propose(lid, a, b, "a", send=[ra[0]], receive=[rb[0]]).status_code == 400, "player-for-player is blocked for an eliminated team")
check(pay(lid, a, 2).status_code == 200, "")
resp = propose(lid, a, b, "a", send=[ra[0]], receive=[rb[0]])
check(resp.status_code == 400 and "future draft value" in resp.json()["detail"], "still blocked after paying: " + resp.text)
resp = propose(lid, a, b, "a", send=[ra[1]], receive=[rb[1]], receive_picks=[(2, 1, b)])
check(resp.status_code == 400 and "dues" in resp.json()["detail"], "b must secure 2 for the pick it sends: " + resp.text)
check(pay(lid, b, 2).status_code == 200, "")
resp = propose(lid, a, b, "a", send=[ra[1]], receive=[rb[1]], receive_picks=[(2, 1, b)])
check(resp.status_code == 200 and resp.json()["status"] == "proposed", "dues paid -> ordinary review: " + resp.text)
check(resp.json()["eligibility"]["approval_requirements"] == [trade_eligibility.STANDARD_TRADE_APPROVAL], "")
check(respond(resp.json()["id"], b, "b").json()["status"] == "accepted", "")
check(owner_of(ra[1], lid) == b, "player moved")
# A is eliminated, unpaid for a later season: a season-3 pick it sends needs 3 secured too.
resp = propose(lid, a, c, "a", send=[ra[2]], receive=[roster(c)[0]], receive_picks=[(2, 1, c)])
check(pay(lid, c, 2).status_code == 200, "")
pay(lid, c, 3)
resp = propose(lid, a, c, "a", send=[ra[2]], receive=[roster(c)[0]], send_picks=[(3, 1, a)], receive_picks=[(2, 1, c)])
check(resp.status_code == 400 and "3" in resp.json()["detail"] and "dues" in resp.json()["detail"],
      "extra outgoing season needs its own dues first: " + resp.text)
check(pay(lid, a, 3).status_code == 200, "")
check(pay(lid, a, 2, ref="x").status_code == 400, "season 2 is not charged twice")
# Eliminated team sends picks and no valuation policy exists: it goes to the league (majority only, no commissioner).
resp = propose(lid, a, c, "a", send=[ra[2]], receive=[roster(c)[0]], send_picks=[(3, 1, a)], receive_picks=[(2, 1, c)])
check(resp.status_code == 200 and resp.json()["status"] == "voting", "pick-sending eliminated trade goes to a vote: " + resp.text)
tid = resp.json()["id"]
check(resp.json()["eligibility"]["approval_requirements"] == [trade_eligibility.ELIMINATED_PICK_TRADE_VOTE], resp.text)
check(resp.json()["eligibility"]["status"] == "AWAITING_LEAGUE_VOTE", "")
check(decide(tid, "commish").status_code == 400, "no commissioner sign-off is involved")
check(vote(tid, b, "b").status_code == 200 and status_of(tid) == "voting", "not yet a majority")
resp = vote(tid, d, "d")
check(resp.status_code == 200 and resp.json()["status"] == "accepted", "majority executes it: " + resp.text)
# With an approved valuation the vote is not needed.
trade_eligibility.set_future_value_evaluator(lambda *args: True)
try:
    resp = propose(lid, a, c, "a", send=[ra[3]], receive=[roster(c)[1]], send_picks=[(3, 2, a)], receive_picks=[(2, 2, c)])
    check(resp.status_code == 200 and resp.json()["status"] == "proposed", "with a valuation it's ordinary review: " + resp.text)
finally:
    trade_eligibility.set_future_value_evaluator(None)

# ---- Vote thresholds ----------------------------------------------------------------------------------------------------
def pick_vote_trade(lid, a, c):
    """An eliminated team a sends a pick to c: no valuation policy, so the league votes on it."""
    ra = roster(a)
    for team, season in ((a, 2), (a, 3), (c, 2)):
        pay(lid, team, season)
    resp = propose(lid, a, c, "a", send=[ra[2]], receive=[roster(c)[0]], send_picks=[(3, 1, a)], receive_picks=[(2, 1, c)])
    assert resp.status_code == 200 and resp.json()["status"] == "voting", resp.text
    return resp.json()


lid, (a, b, c, d) = make_league("VoteCfg", "PAID")
eliminate(lid, a)
check(client.patch(f"/leagues/{lid}/settings", json={"trade_vote_extra_votes": 0}, headers=HDR["commish"]).status_code == 422,
      "at least one vote past half")
check(client.patch(f"/leagues/{lid}/settings", json={"trade_vote_extra_votes": 2}, headers=HDR["a"]).status_code == 403, "")
check(client.patch(f"/leagues/{lid}/settings", json={"trade_vote_extra_votes": 2}, headers=HDR["commish"]).json()["trade_vote_extra_votes"] == 2,
      "configurable")
t = pick_vote_trade(lid, a, c)
check(t["majority_needed"] == 4, t)
check(vote(t["id"], b, "b").status_code == 200 and vote(t["id"], c, "c").status_code == 200 and status_of(t["id"]) == "voting",
      "three of four is under the configured threshold")
check(vote(t["id"], d, "d").json()["status"] == "accepted", "four of four")

lid, (a, b, c, d) = make_league("CommishVote", "PAID")
db.get(Team, a).owner_user_id = users["commish"].id          # the commissioner manages team a
db.commit()
eliminate(lid, a)
ra = roster(a)
resp = propose(lid, a, b, "commish", send=[ra[0]], receive=[roster(b)[0]], receive_picks=[(2, 1, b)])
check(resp.status_code == 400 and "dues" in resp.json()["detail"], resp.text)
for team, season in ((a, 2), (a, 3), (c, 2)):
    pay(lid, team, season)
resp = propose(lid, a, c, "commish", send=[ra[2]], receive=[roster(c)[0]], send_picks=[(3, 1, a)], receive_picks=[(2, 1, c)])
check(resp.status_code == 200 and resp.json()["status"] == "voting", resp.text)
t = resp.json()
check(t["majority_needed"] == 4 and t["eligibility"]["status"] == "AWAITING_LEAGUE_VOTE", "commissioner trades need half + 2: " + str(t))
check(decide(t["id"], "commish").status_code == 400, "no commissioner sign-off on their own trade")
check(vote(t["id"], b, "b").status_code == 200 and vote(t["id"], c, "c").status_code == 200 and status_of(t["id"]) == "voting",
      "three of four (the ordinary threshold) is not enough")
check(vote(t["id"], d, "d").json()["status"] == "accepted", "half + 2 executes it")
check(client.patch(f"/leagues/{lid}/settings", json={"commissioner_trade_vote_extra_votes": 1}, headers=HDR["commish"]).status_code == 200, "")
check(db.get(League, lid).commissioner_trade_vote_extra_votes == 1, "")

# ---- Future draft pick redraft: eliminated teams can't sign, drop or claim ------------------------------------------
lid, (a, b, c, d) = make_league("FprElim", "FREE", "future_pick_redraft")
ra, rb = roster(a), roster(b)
eliminate(lid, a)
for path, aid, what in (("free-agency/drop", ra[0], "drop"), ("free-agency/add", rb[0], "sign"), ("waivers/claim", rb[0], "claim")):
    resp = client.post(f"/teams/{a}/{path}", json={"draftable_asset_id": aid}, headers=HDR["a"])
    check(resp.status_code == 400 and "future draft pick" in resp.json()["detail"], f"eliminated team can't {what}: " + resp.text)
check(client.post(f"/teams/{b}/free-agency/drop", json={"draftable_asset_id": rb[0]}, headers=HDR["b"]).status_code == 200,
      "a team still alive can drop")
resp = propose(lid, a, b, "a", send=[ra[1]], receive=[rb[1]])
check(resp.status_code == 400 and "future draft value" in resp.json()["detail"], "trade rules unchanged: " + resp.text)

# ---- Traditional redraft: eliminated teams are locked (FREE) or pay next season's dues first (PAID) ---------------------
lid, (a, b, c, d) = make_league("FreeTrad", "FREE", "traditional_redraft")
ra, rb = roster(a), roster(b)
check(propose(lid, a, b, "a", send=[ra[0]], receive=[rb[0]]).status_code == 200, "active free redraft team trades normally")
eliminate(lid, a)
resp = propose(lid, a, b, "a", send=[ra[1]], receive=[rb[1]])
check(resp.status_code == 400 and "locked" in resp.json()["detail"], "eliminated free redraft team can't trade: " + resp.text)
resp = propose(lid, b, a, "b", send=[rb[1]], receive=[ra[1]])
check(resp.status_code == 400 and "locked" in resp.json()["detail"], "nor can anyone trade with it: " + resp.text)
resp = client.post(f"/teams/{a}/free-agency/drop", json={"draftable_asset_id": ra[2]}, headers=HDR["a"])
check(resp.status_code == 400 and "locked" in resp.json()["detail"], "no drops: " + resp.text)
check(client.post(f"/teams/{b}/free-agency/drop", json={"draftable_asset_id": rb[2]}, headers=HDR["b"]).status_code == 200,
      "a team still alive can drop")
resp = client.post(f"/teams/{a}/free-agency/add", json={"draftable_asset_id": rb[2]}, headers=HDR["a"])
check(resp.status_code == 400 and "locked" in resp.json()["detail"], "no signings: " + resp.text)
resp = client.post(f"/teams/{a}/waivers/claim", json={"draftable_asset_id": rb[2]}, headers=HDR["a"])
check(resp.status_code == 400 and "locked" in resp.json()["detail"], "no waiver claims: " + resp.text)

lid, (a, b, c, d) = make_league("PaidTrad", "PAID", "traditional_redraft")
eliminate(lid, a)
ra, rb = roster(a), roster(b)
resp = propose(lid, a, b, "a", send=[ra[0]], receive=[rb[0]])
check(resp.status_code == 400 and "locked" not in resp.json()["detail"],
      "paid league: not locked (the dues and future-value rules apply instead): " + resp.text)

# ---- Eliminated, FREE ------------------------------------------------------------------------------------------------
lid, (a, b, c, d) = make_league("FreeElim")
eliminate(lid, a)
ra, rb = roster(a), roster(b)
check(propose(lid, a, b, "a", send=[ra[0]], receive=[rb[0]]).status_code == 400, "player-for-player stays blocked")
# FS-036: a FREE league can no longer trade picks, so an eliminated FREE team (which can only trade by receiving net future
# value) has no route left. The vote + commissioner sign-off path (ELIMINATED_FREE_LEAGUE_TRADE) is unreachable by proposal.
resp = propose(lid, a, b, "a", send=[ra[0]], receive=[rb[0]], receive_picks=[(2, 1, b)])
check(resp.status_code == 400 and "Free leagues can't trade picks" in resp.json()["detail"], "picks are locked in a free league: " + resp.text)
resp = propose(lid, b, a, "b", send=[rb[0]], receive=[ra[0]], send_picks=[(2, 1, b)])
check(resp.status_code == 400 and "Free leagues can't trade picks" in resp.json()["detail"], "in either direction: " + resp.text)
check(owner_of(ra[0], lid) == a, "nothing moved")

# ---- Global enforcement ----------------------------------------------------------------------------------------------
lid, (a, b, c, d) = make_league("Global", "PAID")
ra, rb, rc = roster(a), roster(b), roster(c)
resp = propose(lid, a, b, "a", send=[ra[0]], receive=[rb[0]])
t1 = resp.json()["id"]
resp = propose(lid, a, c, "a", send=[ra[0]], receive=[rc[0]])
t2 = resp.json()["id"]
check(respond(t1, b, "b").json()["status"] == "accepted", "")
resp = respond(t2, c, "c")
check(resp.status_code == 200 and resp.json()["status"] == "blocked" and resp.json()["failure_reason"], "the same player can't move twice: " + resp.text)
check(owner_of(ra[0], lid) == b and owner_of(rc[0], lid) == c, "no asset moved by the blocked trade")

resp = propose(lid, a, b, "a", send=[rb[1]], receive=[ra[1]])
check(resp.status_code == 400, "can't trade a player you don't own")
resp = propose(lid, a, b, "a", send=[ra[1]], receive=[rb[1]])
t3 = resp.json()["id"]
league = db.get(League, lid)
league.trade_deadline = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
db.commit()
check(propose(lid, a, b, "a", send=[ra[2]], receive=[rb[2]]).status_code == 400, "no new trades after the deadline")
resp = respond(t3, b, "b")
check(resp.json()["status"] == "blocked" and "deadline" in resp.json()["failure_reason"], "pending trades die at the deadline: " + resp.text)
check(owner_of(ra[1], lid) == a, "")
league = db.get(League, lid)
league.trade_deadline = None
db.commit()

check(client.patch(f"/leagues/{lid}/settings", json={"pick_trading_enabled": False}, headers=HDR["commish"]).status_code == 200, "")
resp = propose(lid, a, b, "a", send_picks=[(2, 1, a)], receive_picks=[(2, 1, b)])
check(resp.status_code == 400 and "turned off" in resp.json()["detail"], resp.text)

# ---- Franchise continuity --------------------------------------------------------------------------------------------
lid, (a, b, c, d) = make_league("Continuity", "PAID")
check(pay(lid, a, 2).status_code == 200 and pay(lid, b, 2).status_code == 200, "")
db.get(Team, a).owner_user_id = users["stranger"].id          # the manager leaves, a replacement takes the franchise
db.commit()
check(client.get(f"/leagues/{lid}/dues", headers=HDR["stranger"]).json()[0]["team_id"] == a, "replacement sees the credit")
resp = propose(lid, a, b, "stranger", send_picks=[(2, 1, a)], receive_picks=[(2, 2, b)])
check(resp.status_code == 200, "the replacement trades on the prepaid season without paying again: " + resp.text)
check(pay(lid, a, 2, ref="dup", who="commish").status_code == 400, "and isn't charged again")
check(propose(lid, a, b, "a", send_picks=[(2, 3, a)], receive_picks=[(2, 3, b)]).status_code == 403, "the old manager lost control")

print(f"trade eligibility smoke test: {checks} checks passed")
