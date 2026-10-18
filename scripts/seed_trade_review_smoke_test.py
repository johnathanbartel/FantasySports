"""
FS-036: the review window for person-to-person trades, through the real HTTP API (FastAPI TestClient) on a throwaway SQLite
file (trade_review_smoke_test.db): accept -> in_review, the clock-driven queue, commissioner and league vetoes, nobody executing
early, no-window leagues, a deposit disputed mid-review, and a game-locked player deferring a trade.

    .venv-api/Scripts/python.exe scripts/seed_trade_review_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("TRADE_REVIEW_SMOKE_DB", "trade_review_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import game_lock, pick_trading, platform_fees, trade_review, trades
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, League, Player, RealTeam, RosterEntry, Team, Trade, TradeVeto, User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()

# This script reads through `db` between API calls; end its read transaction first so SQLite lets the API write.
_request = client.request


def _request_after_commit(*args, **kwargs):
    db.commit()
    return _request(*args, **kwargs)


client.request = _request_after_commit

nhl, _ = seed_hockey_reference_data(db)
real = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
db.add(real)
db.flush()
players = [Player(universe_id=nhl.id, real_team_id=real.id, full_name=f"Skater {i:03d}", position="C") for i in range(400)]
db.add_all(players)
db.flush()
db.add_all([DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id) for p in players])
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "a", "b", "c", "d", "f")}
db.add_all(users.values())
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


HDR = {n: login(u) for n, u in users.items()}
DUES = 5000
checks = 0


def check(cond, msg=""):
    global checks
    checks += 1
    assert cond, msg


def make_league(name, league_type="FREE", plan="AD_SUPPORTED", **settings):
    """Four person-managed teams a-d, a fifth person-managed team f and an AI team e (both added after the draft)."""
    body = {"name": name, "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"], "size": 4,
            "league_type": league_type}
    if league_type == "PAID":
        body["annual_dues_cents"] = DUES
    lid = client.post("/leagues", json=body, headers=HDR["commish"]).json()["id"]
    resp = client.patch(f"/leagues/{lid}/settings", json={"league_format": "dynasty", **settings}, headers=HDR["commish"])
    assert resp.status_code == 200, resp.text
    teams = [Team(league_id=lid, name=f"{name} {n}", owner_user_id=users[n].id) for n in ("a", "b", "c", "d")]
    db.add_all(teams)
    db.commit()
    draft_id = client.post("/drafts/start", json={"league_id": lid}, headers=HDR["commish"]).json()["id"]
    for _ in range(600):
        if client.post(f"/drafts/{draft_id}/autopick").status_code != 200:
            break
    if plan != "AD_SUPPORTED":
        assert client.patch(f"/leagues/{lid}/settings", json={"platform_plan": plan}, headers=HDR["commish"]).status_code == 200
    f = Team(league_id=lid, name=f"{name} f", owner_user_id=users["f"].id)
    e = Team(league_id=lid, name=f"{name} e", owner_user_id=None, is_ai_owned=True)
    db.add_all([f, e])
    db.commit()
    return lid, [t.id for t in teams] + [f.id, e.id]


def roster(tid):
    return sorted(r.draftable_asset_id for r in db.query(RosterEntry).filter_by(team_id=tid))


def owner_of(aid, lid):
    row = (db.query(RosterEntry).join(Team, Team.id == RosterEntry.team_id)
           .filter(Team.league_id == lid, RosterEntry.draftable_asset_id == aid).first())
    return row.team_id if row else None


def swap(lid, me, them, who, i=0, picks=None):
    """a trade of one player each (or, with picks=(mine, theirs), of one pick each)."""
    body = {"proposer_team_id": me, "counterparty_team_id": them}
    if picks:
        body["send_picks"] = [{"season": s, "round": r, "original_team_id": o} for s, r, o in picks[0]]
        body["receive_picks"] = [{"season": s, "round": r, "original_team_id": o} for s, r, o in picks[1]]
    else:
        body["send_asset_ids"], body["receive_asset_ids"] = [roster(me)[i]], [roster(them)[i]]
    resp = client.post(f"/leagues/{lid}/trades", headers=HDR[who], json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def respond(tid, team, who, accept=True):
    return client.post(f"/trades/{tid}/respond", json={"team_id": team, "accept": accept}, headers=HDR[who])


def veto(tid, team, who):
    return client.post(f"/trades/{tid}/veto", json={"team_id": team}, headers=HDR[who])


def cveto(tid, who="commish", reason=None):
    return client.post(f"/trades/{tid}/commissioner-veto", json={"reason": reason}, headers=HDR[who])


def trade(tid):
    db.expire_all()
    return db.get(Trade, tid)


def accept_into_review(lid, me, them, who_me, who_them, i=0, picks=None):
    tid = swap(lid, me, them, who_me, i, picks)
    resp = respond(tid, them, who_them)
    assert resp.status_code == 200, resp.text
    return tid, resp.json()


# ---- Accept -> in_review; the queue runs on the clock -------------------------------------------------------------------
lid, (a, b, c, d, f, e) = make_league("Review")
check(db.get(League, lid).trade_review_hours == 24 and db.get(League, lid).trade_review_mode == "both", "defaults: 24 hours, both")
ra, rb = roster(a), roster(b)
before = datetime.datetime.utcnow()
tid, out = accept_into_review(lid, a, b, "a", "b")
check(out["status"] == "in_review" and out["review_ends_at"] is not None, out)
ends = trade(tid).review_ends_at
check(datetime.timedelta(hours=23, minutes=59) < ends - before < datetime.timedelta(hours=24, minutes=1), "24 hour window")
check(owner_of(ra[0], lid) == a and owner_of(rb[0], lid) == b, "nothing moved on accept")
check(out["league_veto_needed"] == 2 and out["league_veto_count"] == 0, "c, d, f can veto (the AI team can't): " + str(out))
# nobody can execute early
check(trade_review.process_review_queue(db, ends - datetime.timedelta(minutes=1), league_id=lid) == [], "queue does nothing before the window ends")
check(trade(tid).status == "in_review", "")
try:
    trades.execute_trade(db, trade(tid), ends - datetime.timedelta(minutes=1))
    check(False, "early execution")
except ValueError as ex:
    db.rollback()
    check("review window" in str(ex), str(ex))
check(respond(tid, b, "b").status_code == 400, "can't accept again")
check(client.post(f"/trades/{tid}/vote", json={"team_id": c, "vote": "approve"}, headers=HDR["c"]).status_code == 400, "no approving")
check(client.post(f"/trades/{tid}/commissioner-decision", json={"decision": "approve"}, headers=HDR["commish"]).status_code == 400,
      "the commissioner can't approve it early")
check(owner_of(ra[0], lid) == a, "still nothing moved")
done = trade_review.process_review_queue(db, ends + datetime.timedelta(seconds=1), league_id=lid)
db.commit()
check([x["trade_id"] for x in done] == [tid] and done[0]["result"] == "accepted", done)
check(owner_of(ra[0], lid) == b and owner_of(rb[0], lid) == a and trade(tid).status == "accepted", "executed after the window")
check(trade_review.process_review_queue(db, ends + datetime.timedelta(hours=1), league_id=lid) == [], "and only once")

# ---- Commissioner veto ------------------------------------------------------------------------------------------------
ra, rb = roster(a), roster(b)
tid, _ = accept_into_review(lid, a, b, "a", "b", 1)
check(cveto(tid, "a").status_code == 403, "only the commissioner")
check(cveto(tid, "f").status_code == 403, "")
resp = cveto(tid, reason="looks like collusion")
check(resp.status_code == 200 and resp.json()["status"] == "vetoed" and resp.json()["commissioner_vetoed"], resp.text)
check("collusion" in resp.json()["failure_reason"], "the reason is kept")
check(owner_of(ra[1], lid) == a, "nothing moved")
check(trade_review.process_review_queue(db, datetime.datetime.utcnow() + datetime.timedelta(days=3), league_id=lid) == [], "a vetoed trade never executes")
check(cveto(tid).status_code == 400, "only during the review")

# a commissioner who manages a team in the trade is refused; the league can still veto
db.get(Team, d).owner_user_id = users["commish"].id
db.commit()
tid, _ = accept_into_review(lid, a, d, "a", "commish", 2)
resp = cveto(tid)
check(resp.status_code == 400 and "manages a team" in resp.json()["detail"], "conflicted commissioner can't veto: " + resp.text)
check(trade(tid).status == "in_review", "")
check(veto(tid, c, "c").status_code == 200 and veto(tid, f, "f").json()["status"] == "vetoed",
      "the league (b, c, f minus the trade's teams) still can")
db.get(Team, d).owner_user_id = users["d"].id
db.commit()

# ---- League veto: a majority of the uninvolved person-managed teams --------------------------------------------------------
ra, rb = roster(a), roster(b)
tid, _ = accept_into_review(lid, a, b, "a", "b", 3)
check(veto(tid, a, "a").status_code == 400, "a team in the trade can't veto")
check(veto(tid, b, "b").status_code == 400, "")
check(trade_review.veto_majority(db, trade(tid)) == 2, "c, d, f eligible -> 2")
try:
    trade_review.veto_by_league(db, trade(tid), db.get(Team, e), None)
    check(False, "AI team veto")
except ValueError as ex:
    db.rollback()
    check("person-managed" in str(ex), str(ex))
check(client.post(f"/trades/{tid}/veto", json={"team_id": c}, headers=HDR["d"]).status_code == 403, "you can only veto for your own team")
resp = veto(tid, c, "c")
check(resp.status_code == 200 and resp.json()["status"] == "in_review" and resp.json()["league_veto_count"] == 1, resp.text)
resp = veto(tid, c, "c")
check(resp.json()["league_veto_count"] == 1 and resp.json()["status"] == "in_review", "the same team can't veto twice")
check(db.query(TradeVeto).filter_by(trade_id=tid).count() == 1, "")
resp = veto(tid, d, "d")
check(resp.json()["status"] == "vetoed" and resp.json()["league_veto_count"] == 2, "two of three vetoes it: " + resp.text)
check(owner_of(ra[3], lid) == a, "nothing moved")

# one veto short of a majority executes after the window
tid, _ = accept_into_review(lid, a, b, "a", "b", 4)
veto(tid, c, "c")
ends = trade(tid).review_ends_at
done = trade_review.process_review_queue(db, ends + datetime.timedelta(seconds=1), league_id=lid)
db.commit()
check(done and done[0]["result"] == "accepted", "below the majority the trade goes ahead: " + str(done))

# ---- Cancel during review ---------------------------------------------------------------------------------------------
tid, _ = accept_into_review(lid, a, b, "a", "b", 5)
resp = client.post(f"/trades/{tid}/cancel", headers=HDR["b"])
check(resp.status_code == 200 and resp.json()["status"] == "cancelled", "the counterparty can cancel during review: " + resp.text)
tid, _ = accept_into_review(lid, a, b, "a", "b", 5)
check(client.post(f"/trades/{tid}/cancel", headers=HDR["c"]).status_code == 403, "outsiders can't")
check(client.post(f"/trades/{tid}/cancel", headers=HDR["a"]).json()["status"] == "cancelled", "the proposer can")

# ---- Review modes and hours ------------------------------------------------------------------------------------------
check(client.patch(f"/leagues/{lid}/settings", json={"trade_review_mode": "commissioner"}, headers=HDR["commish"]).status_code == 200, "")
tid, _ = accept_into_review(lid, a, b, "a", "b", 6)
check(veto(tid, c, "c").status_code == 400, "no league veto in commissioner mode")
check(cveto(tid).json()["status"] == "vetoed", "")
check(client.patch(f"/leagues/{lid}/settings", json={"trade_review_mode": "league"}, headers=HDR["commish"]).status_code == 200, "")
tid, _ = accept_into_review(lid, a, b, "a", "b", 7)
check(cveto(tid).status_code == 400, "no commissioner veto in league mode")
check(veto(tid, c, "c").status_code == 200 and veto(tid, d, "d").json()["status"] == "vetoed", "")
check(client.patch(f"/leagues/{lid}/settings", json={"trade_review_mode": "none"}, headers=HDR["commish"]).status_code == 200, "")
ra, rb = roster(a), roster(b)
tid = swap(lid, a, b, "a", 8)
check(respond(tid, b, "b").json()["status"] == "accepted" and owner_of(ra[8], lid) == b, "mode none: executes on accept")
check(client.patch(f"/leagues/{lid}/settings", json={"trade_review_mode": "both", "trade_review_hours": 0}, headers=HDR["commish"]).status_code == 200, "")
ra = roster(a)
tid = swap(lid, a, b, "a", 9)
check(respond(tid, b, "b").json()["status"] == "accepted" and owner_of(ra[9], lid) == b, "0 hours: executes on accept, as before")
check(client.patch(f"/leagues/{lid}/settings", json={"trade_review_hours": 72}, headers=HDR["commish"]).status_code == 200, "72 is the limit")
check(client.patch(f"/leagues/{lid}/settings", json={"trade_review_hours": 73}, headers=HDR["commish"]).status_code == 422, "")
check(client.patch(f"/leagues/{lid}/settings", json={"trade_review_mode": "sometimes"}, headers=HDR["commish"]).status_code == 422, "")
check(client.patch(f"/leagues/{lid}/settings", json={"trade_review_hours": 1}, headers=HDR["a"]).status_code == 403, "commissioner only")
# no eligible veto teams: a two-team-only trade among the rest leaves c, d, f; shrink the pool to nothing
for t in (c, d, f):
    db.get(Team, t).owner_user_id = None
    db.get(Team, t).is_ai_owned = True
db.commit()
check(client.patch(f"/leagues/{lid}/settings", json={"trade_review_hours": 2}, headers=HDR["commish"]).status_code == 200, "")
tid, out = accept_into_review(lid, a, b, "a", "b", 10)
check(out["league_veto_needed"] is None, "nobody eligible: the league veto is unavailable")
try:
    trade_review.veto_by_league(db, trade(tid), db.get(Team, c), None)
    check(False, "veto with no eligible team")
except ValueError as ex:
    db.rollback()
    check("eligible" in str(ex), str(ex))

# ---- A deposit disputed mid-review blocks the trade at execution ------------------------------------------------------------
lid, (a, b, c, d, f, e) = make_league("Deposit", "PAID", "AD_FREE")
for t in (a, b):
    check(client.post(f"/leagues/{lid}/teams/{t}/dues", headers=HDR["commish"], json={
        "season_number": 2, "amount_cents": DUES, "status": "paid", "payment_reference": f"d-{t}"}).status_code == 200, "")
    platform_fees.record_payment(db, db.get(League, lid), db.get(Team, t), 2, platform_fees.PLATFORM_FEE_CENTS, "paid", f"f-{t}", "t")
db.commit()
tid, out = accept_into_review(lid, a, b, "a", "b", picks=([(2, 1, a)], [(2, 2, b)]))
check(out["status"] == "in_review" and out["eligibility"]["status"] == "ELIGIBLE_FOR_REVIEW", out)
fee_a = db.query(platform_fees.PlatformSeasonFee).filter_by(team_id=a, season_number=2).one()
platform_fees.set_status(db, db.get(League, lid), fee_a, "disputed", "t")
db.commit()
ends = trade(tid).review_ends_at
done = trade_review.process_review_queue(db, ends + datetime.timedelta(seconds=1), league_id=lid)
db.commit()
check(done and done[0]["result"] == "blocked" and "platform deposit" in done[0]["detail"], done)
check(trade(tid).status == "blocked" and pick_trading.current_owner(db, lid, 2, 1, a) == a, "no pick moved")
check(db.query(platform_fees.PlatformSeasonFee).filter_by(committed_by_trade_id=tid).count() == 0, "a blocked trade commits nothing")

# a healthy pick trade runs through review and commits its backing records on execution
platform_fees.record_payment(db, db.get(League, lid), db.get(Team, a), 2, platform_fees.PLATFORM_FEE_CENTS, "paid", "f-a-again", "t")
db.commit()
tid, out = accept_into_review(lid, a, b, "a", "b", picks=([(2, 1, a)], [(2, 2, b)]))
done = trade_review.process_review_queue(db, trade(tid).review_ends_at + datetime.timedelta(seconds=1), league_id=lid)
db.commit()
check(done and done[0]["result"] == "accepted" and pick_trading.current_owner(db, lid, 2, 1, a) == b, done)
db.expire_all()
check(db.query(platform_fees.PlatformSeasonFee).filter_by(committed_by_trade_id=tid).count() == 2, "both fees committed on execution")

# ---- A game-locked player defers the trade ----------------------------------------------------------------------------------
lid, (a, b, c, d, f, e) = make_league("Locked")
ra = roster(a)
tid, _ = accept_into_review(lid, a, b, "a", "b")
ends = trade(tid).review_ends_at
with patch.object(game_lock, "assert_unlocked", side_effect=ValueError("Skater is in a game right now")):
    done = trade_review.process_review_queue(db, ends + datetime.timedelta(seconds=1), league_id=lid)
db.commit()
check(done and done[0]["result"] == "deferred", done)
check(trade(tid).status == "in_review" and owner_of(ra[0], lid) == a, "deferred, not failed")
done = trade_review.process_review_queue(db, ends + datetime.timedelta(minutes=5), league_id=lid)
db.commit()
check(done and done[0]["result"] == "accepted" and owner_of(ra[0], lid) == b, "runs once the lock lifts")

# ---- Reading a league's trades runs the queue ------------------------------------------------------------------------------
lid, (a, b, c, d, f, e) = make_league("Lazy")
ra = roster(a)
tid, _ = accept_into_review(lid, a, b, "a", "b")
db.get(Trade, tid).review_ends_at = datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
db.commit()
check(client.get(f"/leagues/{lid}/trades", headers=HDR["a"]).json()[0]["status"] == "accepted", "listing the trades executes a finished review")
check(owner_of(ra[0], lid) == b, "")
resp = client.post(f"/leagues/{lid}/trades/process-review", headers=HDR["a"])
check(resp.status_code == 200 and resp.json() == {"processed": []}, resp.text)
check(client.post(f"/leagues/{lid}/trades/process-review", headers=HDR["commish"]).status_code == 200, "")
check(client.post(f"/leagues/{lid}/trades/process-review").status_code in (401, 403), "needs a login")

print(f"trade review smoke test: {checks} checks passed")
