"""
Offline Draft Mode (phase 1: core state + commissioner API) through the real HTTP API (FastAPI TestClient) on a throwaway
SQLite file (offline_draft_smoke_test.db): session setup and permissions, the snake board filled out of order (entry_seq vs
board position), first-commit-wins conflicts (also under concurrent submissions), traded-pick cells, void / undo / edit,
the auction Log Sale rules (min bid, max bid that keeps the roster fillable, full roster, audited override, refunds),
review summary, finalize into rosters and the league's draft, and re-open reverting those writes.

    .venv-api/Scripts/python.exe scripts/seed_offline_draft_smoke_test.py
"""

import datetime
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

db_path = Path(os.environ.get("OFFLINE_DRAFT_SMOKE_DB", "offline_draft_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
os.environ.pop("FSC_GRANT_ALL_PREMIUM", None)
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import offline_draft as od
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    AuctionDraft, Draft, DraftableAsset, DraftPick, League, LeagueUniverse, Player, RealTeam, RosterEntry, Team, Universe, User,
)
from fantasy_sports.platform.db.models.offline_draft import OfflineAuditLog, OfflineDraftPick, OfflineDraftSession
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app, follow_redirects=False)
db = SessionLocal()
checks = 0


def check(cond, msg):
    global checks
    checks += 1
    print(("ok    " if cond else "FAIL  ") + msg)
    if not cond:
        raise SystemExit(1)


# ---- seed: a 4-team NHL league, 3 rounds, 16 skaters in the pool -------------------------------------------------------------
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "manager")}
db.add_all(users.values())
nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL")
db.add(nhl)
db.flush()
league = League(name="Offline League", commissioner_user_id=users["commish"].id, size=4, draft_rounds=3, draft_type="snake",
                series_length_days=7)
db.add(league)
db.flush()
db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id, defense_mode="group"))
owners = [users["commish"].id, users["manager"].id, None, None]
teams = [Team(league_id=league.id, name=f"Team {i}", owner_user_id=o, is_ai_owned=o is None, program_size="small")
         for i, o in enumerate(owners, start=1)]
school = RealTeam(universe_id=nhl.id, name="Boston", external_ref="BOS")
db.add_all(teams + [school])
db.flush()
assets = []
for i, pos in enumerate(["C", "C", "C", "C", "W", "W", "W", "W", "D", "D", "D", "D", "G", "G", "G", "G"], start=1):
    p = Player(universe_id=nhl.id, real_team_id=school.id, full_name=f"Skater {i}", position=pos)
    db.add(p)
    db.flush()
    a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
    db.add(a)
    assets.append(a)
db.commit()
LID = league.id
T = [t.id for t in teams]
A = [a.id for a in assets]


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


H = {n: login(u) for n, u in users.items()}


def post(path, body=None, who="commish"):
    return client.post(path, json=body or {}, headers=H[who])


def live(sid):
    db.expire_all()
    return db.query(OfflineDraftPick).filter_by(session_id=sid, voided_at=None).all()


# ==== SNAKE ===================================================================================================================
check(post(f"/leagues/{LID}/offline-draft", {"format": "snake"}, who="manager").status_code == 403, "a manager can't create an offline draft")
check(client.post(f"/leagues/{LID}/offline-draft", json={"format": "snake"}).status_code == 401, "no token is a 401")
check(post(f"/leagues/{LID}/offline-draft", {"format": "chess"}).status_code == 400, "an unknown format is refused")
resp = post(f"/leagues/{LID}/offline-draft", {"format": "snake"})
check(resp.status_code == 200, "the commissioner creates a snake session")
snap = resp.json()
S = snap["id"]
base = f"/offline-drafts/{S}"
check(len(snap["join_code"]) == 6 and not set(snap["join_code"]) & set("0O1I"), "join code is 6 chars with no ambiguous ones")
check(snap["status"] == "setup" and snap["rounds"] == 3 and snap["pool_size"] == 16 and len(snap["cells"]) == 12, "3 rounds x 4 teams, pool of 16")
check(post(f"/leagues/{LID}/offline-draft", {"format": "snake"}).status_code == 400, "only one open offline draft per league")
cells = {(c["round"], c["slot"]): c for c in snap["cells"]}
check(cells[(1, 1)]["owner"] == T[0] and cells[(2, 1)]["owner"] == T[3] and cells[(3, 1)]["owner"] == T[0], "the board snakes: round 2 reverses")
check(client.get(base, headers=H["manager"]).status_code == 403, "a manager can't read the console state")

# fill cells out of order: (2,1) first, then (1,1)
r1 = post(f"{base}/picks", {"asset_id": A[0], "round": 2, "slot": 1})
check(r1.status_code == 200, "the commissioner fills a later cell first")
r2 = post(f"{base}/picks", {"asset_id": A[1], "round": 1, "slot": 1})
picks = {(p["round"], p["slot"]): p for p in r2.json()["picks"]}
check(picks[(2, 1)]["entry_seq"] == 1 and picks[(1, 1)]["entry_seq"] == 2, "entry_seq records true order, not board position")
check(picks[(2, 1)]["team_id"] == T[3], "the pick goes to the cell's owner")
v = r2.json()["version"]
check(v > snap["version"], "the version rises on every change")

# conflicts: first commit wins
d = post(f"{base}/picks", {"asset_id": A[0], "round": 1, "slot": 2})
check(d.status_code == 409 and "Already taken by Team 4" in d.json()["detail"], "the same player twice is refused: Already taken by Team 4")
d = post(f"{base}/picks", {"asset_id": A[2], "round": 1, "slot": 1})
check(d.status_code == 409, "a filled cell is refused")
check(post(f"{base}/picks", {"asset_id": 999999, "round": 1, "slot": 2}).status_code == 400, "a player outside the pool is refused")
check(post(f"{base}/picks", {"asset_id": A[2], "round": 9, "slot": 1}).status_code == 400, "a cell off the board is refused")

# traded pick: reassign an empty cell, refuse a filled one
check(post(f"{base}/cell-owner", {"round": 1, "slot": 2, "team_id": T[0]}).status_code == 200, "an empty cell's owner can be reassigned")
check(post(f"{base}/cell-owner", {"round": 1, "slot": 1, "team_id": T[2]}).status_code == 400, "a filled cell's owner can't")
r = post(f"{base}/picks", {"asset_id": A[2], "round": 1, "slot": 2})
check(r.status_code == 200 and {(p["round"], p["slot"]): p for p in r.json()["picks"]}[(1, 2)]["team_id"] == T[0], "the traded cell pays to its new owner")

check(post(f"{base}/cell-owner", {"round": 3, "slot": 1, "team_id": T[1]}).status_code == 200, "a second trade keeps every team at 3 cells")

# void / undo restore the pool
r = post(f"{base}/undo")
check(r.status_code == 200 and [p for p in r.json()["picks"] if p["voided"]][0]["asset_id"] == A[2], "undo last voids the highest entry_seq pick")
pool_ids = {p["asset_id"] for p in client.get(f"{base}?pool=true", headers=H["commish"]).json()["pool"]}
check(A[2] in pool_ids and A[0] not in pool_ids, "the undone player is back in the pool")
check(post(f"{base}/picks", {"asset_id": A[2], "round": 1, "slot": 2}).status_code == 200, "...and can be picked again (even into the same cell)")
voided_id = [p for p in client.get(base, headers=H["commish"]).json()["picks"] if p["asset_id"] == A[1]][0]["id"]
check(post(f"{base}/picks/{voided_id}/void", {"reason": "typo"}).status_code == 200, "a pick can be voided by id")
check(post(f"{base}/picks/{voided_id}/void").status_code == 400, "voiding twice is refused")

# edit = void + new pick, one transaction; a refused edit leaves the old pick alone
r = post(f"{base}/picks", {"asset_id": A[3], "round": 1, "slot": 1})
pid = [p for p in r.json()["picks"] if p["asset_id"] == A[3] and not p["voided"]][0]["id"]
r = post(f"{base}/picks/{pid}/edit", {"asset_id": A[4]})
check(r.status_code == 200 and any(p["asset_id"] == A[4] and not p["voided"] for p in r.json()["picks"]), "an edit swaps the player")
check(any(p["id"] == pid and p["voided"] for p in r.json()["picks"]), "...and keeps the old pick in history, voided")
pid2 = [p for p in r.json()["picks"] if p["asset_id"] == A[4] and not p["voided"]][0]["id"]
r = post(f"{base}/picks/{pid2}/edit", {"asset_id": A[0]})
check(r.status_code == 409, "an edit to a player already taken is refused")
check(any(p["id"] == pid2 and not p["voided"] for p in client.get(base, headers=H["commish"]).json()["picks"]), "...and the original pick is untouched")

# phones: only the pick on the clock, and only while the clock runs (engine rule used by phase 3)
db.expire_all()
sess = db.get(OfflineDraftSession, S)
try:
    od.make_pick(db, sess, A[5], 3, 4, "phone", enforce_turn=True)
    check(False, "a phone pick while the clock isn't running is refused")
except od.OfflineDraftError:
    check(True, "a phone pick while the clock isn't running is refused")
db.rollback()
check(post(f"{base}/status", {"status": "live"}).status_code == 200 and post(f"{base}/entry-mode", {"entry_mode": "live"}).status_code == 200,
      "start the draft and switch to live mode")
state = client.get(base, headers=H["commish"]).json()
clock = state["on_the_clock"]
db.expire_all()
sess = db.get(OfflineDraftSession, S)
other = (3, 4) if (clock["round"], clock["slot"]) != (3, 4) else (3, 3)
try:
    od.make_pick(db, sess, A[5], other[0], other[1], "phone", enforce_turn=True)
    check(False, "a phone pick off the clock is refused")
except od.OfflineDraftError:
    check(True, "a phone pick off the clock is refused")
db.rollback()
check(post(f"{base}/status", {"status": "paused"}).status_code == 200 and post(f"{base}/status", {"status": "live"}).status_code == 200, "pause and resume")
check(post(f"{base}/status", {"status": "setup"}).status_code == 400, "an impossible status change is refused")

# concurrency: 8 submissions for the same player into different cells -> exactly one wins, one player on one team
free_cells = [(c["round"], c["slot"]) for c in state["cells"] if (c["round"], c["slot"]) not in
              {(p["round"], p["slot"]) for p in state["picks"] if not p["voided"]}]
target = A[10]
codes = []


def worker(cell):
    c = TestClient(app, follow_redirects=False)
    codes.append(c.post(f"{base}/picks", json={"asset_id": target, "round": cell[0], "slot": cell[1]}, headers=H["commish"]).status_code)


threads = [threading.Thread(target=worker, args=(cell,)) for cell in free_cells[:8]]
[t.start() for t in threads]
[t.join() for t in threads]
check(sorted(codes) == [200] + [409] * 7, f"8 concurrent submissions for one player: one wins, seven get 409 ({sorted(codes)})")
check(sum(1 for p in live(S) if p.draftable_asset_id == target) == 1, "the player is on exactly one team")
check(db.query(OfflineAuditLog).filter_by(session_id=S, action="pick").count() >= 5, "picks are in the audit log")

# fill the rest, review, finalize
state = client.get(base, headers=H["commish"]).json()
filled = {(p["round"], p["slot"]) for p in state["picks"] if not p["voided"]}
used = {p["asset_id"] for p in state["picks"] if not p["voided"]}
spare = [a for a in A if a not in used]
for cell in [(c["round"], c["slot"]) for c in state["cells"] if (c["round"], c["slot"]) not in filled][:-1]:
    assert post(f"{base}/picks", {"asset_id": spare.pop(0), "round": cell[0], "slot": cell[1]}).status_code == 200
check(post(f"{base}/finalize", {}).status_code == 400, "can't finalize before review")
check(post(f"{base}/status", {"status": "review"}).status_code == 200, "move to review")
rv = client.get(base, headers=H["commish"]).json()["review"]
check(len(rv["incomplete_rosters"]) >= 1 and rv["total_picks"] == 11, "review lists the incomplete roster")
check(post(f"{base}/finalize", {}).status_code == 400, "finalize refuses incomplete rosters by default")
check(post(f"{base}/finalize", {"allow_incomplete": True}).status_code == 200, "...and accepts them when told to")
db.expire_all()
check(db.query(RosterEntry).filter(RosterEntry.team_id.in_(T), RosterEntry.acquired_via == "draft").count() == 11, "rosters are written like an online draft")
check(db.query(Draft).filter_by(league_id=LID, status="complete").count() == 1 and db.query(DraftPick).count() == 11, "the league's draft is complete with its picks")
check(post(f"{base}/picks", {"asset_id": spare[0] if spare else A[15], "round": 3, "slot": 4}).status_code == 400, "a finalized draft takes no more entries")
check(post(f"{base}/reopen").status_code == 200, "the commissioner re-opens inside the window")
db.expire_all()
check(db.query(RosterEntry).filter(RosterEntry.team_id.in_(T)).count() == 0 and db.query(Draft).filter_by(league_id=LID).count() == 0,
      "re-opening reverts the roster and draft writes")
check(post(f"{base}/picks", {"asset_id": spare[0] if spare else A[15], "round": 3, "slot": 4}).status_code == 200, "the board is editable again")
sess = db.get(OfflineDraftSession, S)
sess.status = "review"
db.commit()
post(f"{base}/finalize", {"allow_incomplete": True})
db.expire_all()
sess = db.get(OfflineDraftSession, S)
sess.finalized_at = datetime.datetime.utcnow() - datetime.timedelta(hours=25)
db.commit()
check(post(f"{base}/reopen").status_code == 400, "after the window, re-open is refused")

# display token: read-only feed, rotates
tok = client.get(base, headers=H["commish"]).json()["display_token"]
dsp = client.get(f"/offline-drafts/display/{tok}")
check(dsp.status_code == 200 and "display_token" not in dsp.json() and dsp.json()["league_name"] == "Offline League", "the Big Screen feed needs only the token and leaks nothing private")
new = post(f"{base}/rotate-display-token").json()["display_token"]
check(new != tok and client.get(f"/offline-drafts/display/{tok}").status_code == 404, "rotating the token kills the old link")

# ==== AUCTION =================================================================================================================
# clear the league's roster so the auction has a clean pool
db.query(RosterEntry).filter(RosterEntry.team_id.in_(T)).delete(synchronize_session=False)
db.query(DraftPick).delete()
db.query(Draft).delete()
db.commit()
resp = post(f"/leagues/{LID}/offline-draft", {"format": "auction", "budget": 100, "min_bid": 1})
check(resp.status_code == 200, "an auction session starts once the snake one is finalized")
S2 = resp.json()["id"]
ab = f"/offline-drafts/{S2}"
check(post(f"{ab}/picks", {"asset_id": A[0], "round": 1, "slot": 1}).status_code == 400, "an auction has no cells")
check(post(f"{ab}/sales", {"asset_id": A[0], "team_id": T[0], "price": 0}).status_code == 400, "below the minimum is refused")
check(post(f"{ab}/sales", {"asset_id": A[0], "team_id": T[0], "price": 99}).status_code == 400, "above the max bid is refused (100 - 2 open slots x 1)")
r = post(f"{ab}/sales", {"asset_id": A[0], "team_id": T[0], "price": 98})
t0 = [t for t in r.json()["teams"] if t["team_id"] == T[0]][0]
check(r.status_code == 200 and t0["remaining"] == 2 and t0["open_slots"] == 2 and t0["max_bid"] == 1, "a max-price sale leaves exactly min bid per open slot")
check(post(f"{ab}/sales", {"asset_id": A[1], "team_id": T[0], "price": 2}).status_code == 400, "so the next bid can't exceed 1")
check(post(f"{ab}/sales", {"asset_id": A[0], "team_id": T[1], "price": 5}).status_code == 409, "a sold player can't be sold twice")
check(post(f"{ab}/sales", {"asset_id": A[1], "team_id": T[0], "price": 1}).status_code == 200, "a min-price sale fits")
r = post(f"{ab}/sales", {"asset_id": A[2], "team_id": T[0], "price": 1})
check(r.status_code == 200, "the last slot fills")
check(post(f"{ab}/sales", {"asset_id": A[3], "team_id": T[0], "price": 1}).status_code == 400, "a full roster is refused")
check(post(f"{ab}/sales", {"asset_id": A[3], "team_id": T[0], "price": 1, "override": True}).status_code == 200, "...unless the commissioner overrides")
check(db.query(OfflineAuditLog).filter_by(session_id=S2, action="override_pick").count() == 1, "the override is in the audit log")
sold = [p for p in r.json()["picks"] if p["asset_id"] == A[0]][0]
r = post(f"{ab}/picks/{sold['id']}/void")
t0 = [t for t in r.json()["teams"] if t["team_id"] == T[0]][0]
check(t0["spent"] == 3 and t0["remaining"] == 97, "voiding a sale refunds the budget")
# edit a sale's price
other_sale = [p for p in r.json()["picks"] if p["asset_id"] == A[1] and not p["voided"]][0]
r = post(f"{ab}/picks/{other_sale['id']}/edit", {"price": 40})
check(r.status_code == 200 and [p for p in r.json()["picks"] if p["asset_id"] == A[1] and not p["voided"]][0]["price"] == 40, "edit changes a sale's price")
rv = post(f"{ab}/status", {"status": "review"}).json()["review"]
check(rv["overrides"] == 1 and len(rv["incomplete_rosters"]) == 3, "review counts overrides and lists incomplete rosters")
check(post(f"{ab}/finalize", {"allow_incomplete": True}).status_code == 200, "finalize the auction")
db.expire_all()
check(db.query(RosterEntry).filter(RosterEntry.acquired_via == "auction").count() == 3 and
      db.query(AuctionDraft).filter_by(league_id=LID, status="complete").count() == 1, "auction rosters written and the auction marked complete")
check(post(f"{ab}/reopen").status_code == 200, "auction re-open works")
db.expire_all()
check(db.query(RosterEntry).filter(RosterEntry.team_id.in_(T)).count() == 0 and db.query(AuctionDraft).count() == 0, "...and reverts its writes")

# league deletion removes the offline tables
check(client.delete(f"/leagues/{LID}", headers=H["commish"]).status_code == 200, "the commissioner deletes the league")
db.expire_all()
check(db.query(OfflineDraftSession).count() == 0 and db.query(OfflineDraftPick).count() == 0 and db.query(OfflineAuditLog).count() == 0,
      "its offline drafts go with it")

print(f"\nAll {checks} offline-draft checks passed.")
db.close()
engine.dispose()
if db_path.exists():
    db_path.unlink()
