"""
Offline Draft Mode phase 3 through the real HTTP API on a throwaway SQLite file (offline_phone_smoke_test.db): join code and
team PINs (wrong-PIN lock, unlock, rate limit, one device per team and the replaced device is signed out), snake picks from
phones (only the team on the clock; free-entry self-enter switch; queue and autopick on timeout; pause freezes the clock),
auction nominate / bid from phones (turn order, increment, max bid that keeps the roster fillable, anti-snipe, the server
closing the lot, pause, restart recovery), and tokens expiring at finalize.

    .venv-api/Scripts/python.exe scripts/seed_offline_phone_smoke_test.py
"""

import datetime
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

db_path = Path(os.environ.get("OFFLINE_PHONE_SMOKE_DB", "offline_phone_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
os.environ.pop("FSC_GRANT_ALL_PREMIUM", None)
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import offline_draft as od
from fantasy_sports.platform import offline_live as live
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    Draft, DraftableAsset, DraftPick, League, LeagueUniverse, Player, RealTeam, RosterEntry, Team, Universe, User,
)
from fantasy_sports.platform.db.models.offline_draft import (
    OfflineAuctionLot, OfflineDraftPick, OfflineDraftSession, OfflineTeamClaim,
)
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


# ---- seed: a 4-team NHL league, 16 skaters in the pool ----------------------------------------------------------------------
users = {"commish": User(email="commish@example.com", display_name="commish"),
         "manager": User(email="manager@example.com", display_name="manager")}
db.add_all(users.values())
nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL")
db.add(nhl)
db.flush()
league = League(name="Phone League", commissioner_user_id=users["commish"].id, size=4, draft_rounds=3, draft_type="snake",
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


def phone(token, method, path, body=None):
    return client.request(method, path, json=body, headers={"X-Device-Token": token})


def join(code, team, pin):
    return client.post(f"/offline-join/{code}/claim", json={"team_id": team, "pin": pin})


def reload_session(sid):
    db.expire_all()
    return db.get(OfflineDraftSession, sid)


# ==== SNAKE ===================================================================================================================
snap = post(f"/leagues/{LID}/offline-draft", {"format": "snake", "rounds": 3}).json()
S, CODE = snap["id"], snap["join_code"]
base = f"/offline-drafts/{S}"
check(post(f"{base}/pins", {}, who="manager").status_code == 403, "a manager can't generate PINs")
check(client.get("/offline-join/ZZZZZZ").status_code == 404, "an unknown join code is a 404")
info = client.get(f"/offline-join/{CODE.lower()}").json()
check(len(info["teams"]) == 4 and not any(t["claimed"] for t in info["teams"]), "the join page lists the teams (code is case-insensitive)")
check(join(CODE, T[0], "1234").status_code == 400, "claiming before PINs exist is refused")
P = {int(k): v for k, v in post(f"{base}/pins", {}).json()["pins"].items()}
check(len(P) == 4 and all(len(v) == 4 and v.isdigit() for v in P.values()), "the commissioner gets one 4-digit PIN per team")
db.expire_all()
check(all("$" in c.pin_hash for c in db.query(OfflineTeamClaim).filter_by(session_id=S)), "PINs are stored hashed")


def wrong(t):
    return "0000" if P[t] != "0000" else "1111"


check(join(CODE, T[1], wrong(T[1])).status_code == 403, "a wrong PIN is refused")
for _ in range(3):
    join(CODE, T[1], wrong(T[1]))
ok = join(CODE, T[1], P[T[1]])
check(ok.status_code == 200 and ok.json()["status"] == "claimed", "the right PIN after four misses still works")
for _ in range(5):
    r = join(CODE, T[2], wrong(T[2]))
check(r.status_code == 403 and "locked" in r.json()["detail"], "five wrong PINs lock the team")
check(join(CODE, T[2], P[T[2]]).status_code == 400, "a locked team refuses even the right PIN")
check(post(f"{base}/unlock", {"team_id": T[2]}).status_code == 200 and join(CODE, T[2], P[T[2]]).status_code == 200, "the commissioner unlocks it")

tok = {t: join(CODE, t, P[t]).json()["token"] for t in T}
old = tok[T[0]]
tok[T[0]] = join(CODE, T[0], P[T[0]]).json()["token"]
check(phone(old, "GET", "/offline-phone/state").status_code == 401, "a new claim signs the old device out")
check(phone(tok[T[0]], "GET", "/offline-phone/state").status_code == 200, "...and the new device works")
cl = client.get(base, headers=H["commish"]).json()["claims"]
check(any(c["device_replaced_at"] for c in cl), "the commissioner can see a device was replaced")
check(client.get("/offline-phone/state").status_code == 401 and phone("nope", "GET", "/offline-phone/state").status_code == 401, "no / bad token is a 401")

sv = phone(tok[T[0]], "GET", "/offline-phone/state").json()
check(sv["me"]["team_id"] == T[0] and not sv["me"].get("can_pick"), "a phone can't pick while the draft is in setup")
check(post(f"{base}/settings", {"pick_seconds": 30}).status_code == 200, "set a 30s pick clock")
post(f"{base}/status", {"status": "live"})
post(f"{base}/entry-mode", {"entry_mode": "live"})
sv = phone(tok[T[0]], "GET", "/offline-phone/state").json()
check(sv["me"]["can_pick"] and sv["me"]["on_the_clock"] and sv["on_the_clock"] == {"round": 1, "slot": 1}, "team 1 is on the clock at 1.1")
check(sv["live"]["clock"]["deadline"] is not None and sv["live"]["server_time"], "the server clock and server time are in the state")
sv2 = phone(tok[T[1]], "GET", "/offline-phone/state").json()
check(sv2["me"]["on_deck"] and not sv2["me"]["can_pick"], "team 2 is on deck, not on the clock")
check(phone(tok[T[1]], "POST", "/offline-phone/pick", {"asset_id": A[0]}).status_code == 400, "a phone can't pick for a team that isn't on the clock")
check(phone(tok[T[0]], "POST", "/offline-phone/pick", {"asset_id": A[0]}).status_code == 200, "the team on the clock picks from its phone")
check(client.get(base, headers=H["commish"]).json()["picks"][0]["source"] == "phone", "the pick is recorded as a phone pick")
check(phone(tok[T[1]], "POST", "/offline-phone/pick", {"asset_id": A[0]}).status_code == 409, "an already-taken player is a 409")
q = phone(tok[T[0]], "GET", "/offline-phone/pool?q=skater 1").json()
check(all(p["asset_id"] != A[0] for p in q) and len(q) >= 1, "the phone's search excludes taken players")


def expire_clock(sid):
    s = reload_session(sid)
    c = dict(s.settings_json["clock"])
    c["deadline"] = (datetime.datetime.utcnow() - datetime.timedelta(seconds=5)).isoformat()
    s.settings_json = {**s.settings_json, "clock": c}
    db.commit()


expire_clock(S)
sv = phone(tok[T[1]], "GET", "/offline-phone/state").json()
check(sv["live"]["clock"]["expired"] and len([p for p in sv["picks"] if not p["voided"]]) == 1, "with autopick off, an expired clock waits for the commissioner")
check(phone(tok[T[1]], "POST", "/offline-phone/queue", {"asset_ids": [A[7], A[7], 999999, A[6]]}).status_code == 200, "a team saves its queue")
check(phone(tok[T[1]], "GET", "/offline-phone/state").json()["me"]["queue"] == [A[7], A[6]], "the queue is de-duplicated and limited to the pool")
check(post(f"{base}/settings", {"autopick": True}).status_code == 200, "turn autopick on")
sv = phone(tok[T[1]], "GET", "/offline-phone/state").json()
check(sv["live"]["clock"] is not None, "the clock restarts after a settings change")
expire_clock(S)
sv = phone(tok[T[1]], "GET", "/offline-phone/state").json()
auto = [p for p in sv["picks"] if p["source"] == "autopick"]
check(len(auto) >= 1 and auto[0]["asset_id"] == A[7] and auto[0]["team_id"] == T[1], "on timeout the queue's first player is auto-picked")
check(auto[0]["round"] == 1 and auto[0]["slot"] == 2, "...into the cell that was on the clock")

post(f"{base}/status", {"status": "paused"})
c = reload_session(S).settings_json["clock"]
check(c["deadline"] is None and c["remaining"] > 0, "pause stores the time left")
time.sleep(1.1)
post(f"{base}/status", {"status": "live"})
phone(tok[T[0]], "GET", "/offline-phone/state")
c2 = reload_session(S).settings_json["clock"]
check(c2["deadline"] is not None and c2["remaining"] is None, "resume gives the pick a deadline again")
left = (datetime.datetime.fromisoformat(c2["deadline"]) - datetime.datetime.utcnow()).total_seconds()
check(left <= c["remaining"] + 0.5, f"...with the time that was left ({left:.1f}s <= {c['remaining']:.1f}s)")

post(f"{base}/entry-mode", {"entry_mode": "free"})
cell = phone(tok[T[2]], "GET", "/offline-phone/state").json()["on_the_clock"]
owner = od.cell_owner(reload_session(S), cell["round"], cell["slot"])
check(phone(tok[owner], "POST", "/offline-phone/pick", {"asset_id": A[8]}).status_code == 400, "in free-entry mode a phone is view-only")
post(f"{base}/settings", {"managers_self_enter": True})
check(phone(tok[owner], "POST", "/offline-phone/pick", {"asset_id": A[8]}).status_code == 200, "with self-enter on, the owner of the next cell can enter its own pick")
post(f"{base}/entry-mode", {"entry_mode": "live"})

cell = phone(tok[T[0]], "GET", "/offline-phone/state").json()["on_the_clock"]
owner = od.cell_owner(reload_session(S), cell["round"], cell["slot"])
res = {}


def t_phone():
    res["phone"] = TestClient(app).post("/offline-phone/pick", json={"asset_id": A[9]}, headers={"X-Device-Token": tok[owner]}).status_code


def t_comm():
    res["comm"] = TestClient(app).post(f"{base}/picks", json={"asset_id": A[9], "round": cell["round"], "slot": cell["slot"]},
                                       headers=H["commish"]).status_code


th = [threading.Thread(target=t_phone), threading.Thread(target=t_comm)]
[t.start() for t in th]
[t.join() for t in th]
check(sorted(res.values()) == [200, 409], f"phone and commissioner racing for one cell: first commit wins ({res})")
db.expire_all()
check(db.query(OfflineDraftPick).filter_by(session_id=S, draftable_asset_id=A[9], voided_at=None).count() == 1, "the player is on exactly one team")

post(f"{base}/status", {"status": "review"})
check(post(f"{base}/finalize", {"allow_incomplete": True}).status_code == 200, "finalize")
check(phone(tok[T[0]], "GET", "/offline-phone/state").status_code == 401, "device tokens stop working at finalize")
check(client.get(f"/offline-join/{CODE}").status_code == 404, "...and the join code closes")

# ==== AUCTION =================================================================================================================
db.query(RosterEntry).filter(RosterEntry.team_id.in_(T)).delete(synchronize_session=False)
db.query(DraftPick).delete()
db.query(Draft).delete()
db.commit()
live._attempts.clear()
snap = post(f"/leagues/{LID}/offline-draft", {"format": "auction", "budget": 100, "min_bid": 1, "rounds": 3}).json()
S2, CODE2 = snap["id"], snap["join_code"]
ab = f"/offline-drafts/{S2}"
pins = {int(k): v for k, v in post(f"{ab}/pins", {}).json()["pins"].items()}
tk = {t: client.post(f"/offline-join/{CODE2}/claim", json={"team_id": t, "pin": pins[t]}).json()["token"] for t in T}
post(f"{ab}/status", {"status": "live"})
post(f"{ab}/entry-mode", {"entry_mode": "live"})
sv = phone(tk[T[0]], "GET", "/offline-phone/state").json()
check(sv["me"]["can_nominate"] and sv["live"]["nominator"] == T[0] and sv["live"]["lot"] is None, "team 1 nominates first")
check(phone(tk[T[1]], "POST", "/offline-phone/nominate", {"asset_id": A[0]}).status_code == 400, "out-of-turn nomination is refused")
check(phone(tk[T[0]], "POST", "/offline-phone/nominate", {"asset_id": A[0], "opening_bid": 99}).status_code == 400, "an opening bid over the max (98) is refused")
check(phone(tk[T[0]], "POST", "/offline-phone/nominate", {"asset_id": A[0], "opening_bid": 5}).status_code == 200, "team 1 nominates a player at $5")
check(phone(tk[T[0]], "POST", "/offline-phone/nominate", {"asset_id": A[1]}).status_code == 400, "only one lot at a time")
sv = phone(tk[T[1]], "GET", "/offline-phone/state").json()
lot = sv["live"]["lot"]
check(lot["current_bid"] == 5 and lot["bidder"] == T[0] and sv["me"]["can_bid"], "everyone sees the lot at $5 with team 1 high")
check(phone(tk[T[0]], "POST", "/offline-phone/bid", {"amount": 9}).status_code == 400, "the high bidder can't outbid themselves")
check(phone(tk[T[1]], "POST", "/offline-phone/bid", {"amount": 5}).status_code == 400, "a bid must beat the current bid by the increment")
check(phone(tk[T[1]], "POST", "/offline-phone/bid", {"amount": 99}).status_code == 400, "a bid above the max bid (98) is refused")
check(phone(tk[T[1]], "POST", "/offline-phone/bid", {"amount": 6}).status_code == 200, "a valid bid is accepted")


def set_lot(sid, seconds):
    db.expire_all()
    l = db.query(OfflineAuctionLot).filter_by(session_id=sid, status="open").one()
    l.ends_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
    db.commit()


def open_lot(sid):
    db.expire_all()
    return db.query(OfflineAuctionLot).filter_by(session_id=sid, status="open").one()


set_lot(S2, 3)
check(phone(tk[T[2]], "POST", "/offline-phone/bid", {"amount": 7}).status_code == 200, "a late bid is accepted")
check((open_lot(S2).ends_at - datetime.datetime.utcnow()).total_seconds() > 8.5, "...and the end is pushed to the reset window")
post(f"{ab}/status", {"status": "paused"})
l = open_lot(S2)
check(l.ends_at is None and l.paused_remaining > 5, "pause stores the lot's time left")
check(phone(tk[T[1]], "POST", "/offline-phone/bid", {"amount": 8}).status_code == 400, "no bids while paused")
post(f"{ab}/status", {"status": "live"})
l = open_lot(S2)
check(l.ends_at is not None and l.paused_remaining is None, "resume restarts the lot's clock")
check(post(f"{ab}/lot/extend", {"seconds": 20}).status_code == 200, "the commissioner can extend the lot")

set_lot(S2, -2)
sv = phone(tk[T[0]], "GET", "/offline-phone/state").json()
sold = [p for p in sv["picks"] if not p["voided"]]
check(sv["live"]["lot"] is None and len(sold) == 1 and sold[0]["team_id"] == T[2] and sold[0]["price"] == 7, "when time is up the server sells it to the high bidder at $7")
check([t for t in sv["teams"] if t["team_id"] == T[2]][0]["remaining"] == 93, "the winner's budget is debited")
check(sv["live"]["nominator"] == T[1], "the nomination turn rotates to the next team")
check(phone(tk[T[1]], "POST", "/offline-phone/bid", {"amount": 8}).status_code == 400, "no lot, no bids")

check(phone(tk[T[1]], "POST", "/offline-phone/nominate", {"asset_id": A[1], "opening_bid": 3}).status_code == 200, "team 2 nominates")
set_lot(S2, -5)
n = live.close_expired_on_boot(SessionLocal)
db.expire_all()
check(n == 1 and db.query(OfflineAuctionLot).filter_by(session_id=S2, status="sold").count() == 2, "boot closes a lot whose time ran out")

check(phone(tk[T[2]], "POST", "/offline-phone/nominate", {"asset_id": A[2]}).status_code == 200, "team 3 nominates at the minimum")
check(post(f"{ab}/lot/cancel").status_code == 200, "the commissioner cancels a lot")
check(not [p for p in client.get(ab, headers=H["commish"]).json()["picks"] if p["asset_id"] == A[2]], "...and nothing is sold")
check(post(f"{ab}/lot/nominate", {"team_id": T[0], "asset_id": A[3], "opening_bid": 2}).status_code == 200, "the commissioner can open a lot for any team")
check(post(f"{ab}/lot/force-sell").status_code == 200, "...and force-sell it")
db.expire_all()
check(db.query(OfflineDraftPick).filter_by(session_id=S2, draftable_asset_id=A[3], voided_at=None).count() == 1, "forced sale recorded")

post(f"{ab}/lot/nominate", {"team_id": T[3], "asset_id": A[4], "opening_bid": 1})
codes = []


def bidder(t):
    codes.append(TestClient(app).post("/offline-phone/bid", json={"amount": 10}, headers={"X-Device-Token": tk[t]}).status_code)


th = [threading.Thread(target=bidder, args=(T[i % 3],)) for i in range(6)]
[t.start() for t in th]
[t.join() for t in th]
check(codes.count(200) == 1, f"six simultaneous $10 bids: exactly one accepted ({sorted(codes)})")

live._attempts.clear()
codes = [client.post(f"/offline-join/{CODE2}/claim", json={"team_id": 999999, "pin": "0000"}).status_code for _ in range(25)]
check(codes[0] == 400 and codes.count(429) >= 4, "claim attempts are rate limited")

print(f"\nAll {checks} offline-phone checks passed.")
db.close()
engine.dispose()
if db_path.exists():
    db_path.unlink()
