"""
Manual draft order (platform/draft_order.py), through the real HTTP API (FastAPI TestClient) on a throwaway SQLite file
(draft_order_smoke_test.db):

  * league creation with draft_order_mode 'manual': teams-setup stores the positions, a bad or half-given set is refused,
    no positions given means slot order
  * PUT /leagues/{id}/draft-order: commissioner only, every team once, switching back to random clears the positions,
    a settings email is queued when the mode changes
  * starting the draft uses the saved order (and ignores the draft screen's randomize flag and its own team_order); a snake
    draft reverses it in round 2; a random league still honours the draft screen's order
  * once the draft has started the order is locked

    .venv-api/Scripts/python.exe scripts/seed_draft_order_smoke_test.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

db_path = Path(os.environ.get("DRAFT_ORDER_SMOKE_DB", "draft_order_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import DraftableAsset, EmailOutbox, Player, RealTeam, User
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
players = [Player(universe_id=nhl.id, real_team_id=real.id, full_name=f"Skater {i:03d}", position="C") for i in range(200)]
db.add_all(players)
db.flush()
db.add_all([DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id) for p in players])
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "other")}
db.add_all(users.values())
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


HDR = {n: login(u) for n, u in users.items()}
checks = 0


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


SIZE = 4


def make_league(name, mode, positions="given"):
    body = {"name": name, "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"], "size": SIZE,
            "draft_order_mode": mode}
    resp = client.post("/leagues", json=body, headers=HDR["commish"])
    assert resp.status_code == 200, resp.text
    league = resp.json()
    items = [{"name": "Commish FC", "is_commissioner": True}] + [{"name": f"AI {i}", "is_ai": True} for i in range(SIZE - 1)]
    if positions == "given":
        for item, pos in zip(items, (3, 1, 4, 2)):
            item["draft_position"] = pos
    elif isinstance(positions, list):
        for item, pos in zip(items, positions):
            item["draft_position"] = pos
    resp = client.post(f"/leagues/{league['id']}/teams-setup", json={"teams": items}, headers=HDR["commish"])
    return league, resp


def teams_of(lid):
    return client.get(f"/leagues/{lid}/teams").json()


def first_round(draft_id):
    picks = client.get(f"/drafts/{draft_id}/picks").json()
    return [p["team_id"] for p in sorted(picks, key=lambda p: p["pick_number"]) if p["round"] == 1]


def second_round(draft_id):
    picks = client.get(f"/drafts/{draft_id}/picks").json()
    return [p["team_id"] for p in sorted(picks, key=lambda p: p["pick_number"]) if p["round"] == 2]


# --- creation: the wizard path -------------------------------------------------------------------------------------------
default_league, _ = make_league("Default", "random", positions=None)
check(default_league["draft_order_mode"] == "random", "a new league drafts in random order by default")

league, resp = make_league("Manual", "manual")
check(resp.status_code == 200, resp.text)
check(league["draft_order_mode"] == "manual", "the mode is stored on the league")
teams = teams_of(league["id"])
by_pos = {t["draft_position"]: t["id"] for t in teams}
check(sorted(by_pos) == [1, 2, 3, 4], f"every team has a position: {by_pos}")
check(next(t for t in teams if t["name"] == "Commish FC")["draft_position"] == 3, "the position follows the slot it was given to")

_, resp = make_league("Dupe", "manual", positions=[1, 1, 2, 3])
check(resp.status_code == 400, "duplicate positions are refused")
_, resp = make_league("Half", "manual", positions=[1, 2])
check(resp.status_code == 400, "giving only some teams a position is refused")
_, resp = make_league("Slot order", "manual", positions=None)
check(resp.status_code == 200 and [t["draft_position"] for t in teams_of(resp.json()["league_id"])] == [1, 2, 3, 4],
      "no positions given means the slot order")
_, resp = make_league("Random ignores", "random", positions=[4, 3, 2, 1])
check(resp.status_code == 200 and all(t["draft_position"] is None for t in teams_of(resp.json()["league_id"])),
      "a random league keeps no positions")

# --- changing the order in League Settings ------------------------------------------------------------------------------
lid = league["id"]
ids = [by_pos[p] for p in (1, 2, 3, 4)]
new_order = list(reversed(ids))
check(client.put(f"/leagues/{lid}/draft-order", json={"mode": "manual", "team_order": new_order}).status_code == 401, "sign-in needed")
check(client.put(f"/leagues/{lid}/draft-order", json={"mode": "manual", "team_order": new_order}, headers=HDR["other"]).status_code == 403,
      "only the commissioner can set the order")
check(client.put(f"/leagues/{lid}/draft-order", json={"mode": "manual", "team_order": new_order[:3]}, headers=HDR["commish"]).status_code == 400,
      "the order must list every team")
check(client.put(f"/leagues/{lid}/draft-order", json={"mode": "manual", "team_order": new_order[:3] + [99999]}, headers=HDR["commish"]).status_code == 400,
      "a team from outside the league is refused")
check(client.put(f"/leagues/{lid}/draft-order", json={"mode": "manual"}, headers=HDR["commish"]).status_code == 400, "manual needs an order")
check(client.put(f"/leagues/{lid}/draft-order", json={"mode": "sideways"}, headers=HDR["commish"]).status_code == 422, "mode is validated")
resp = client.put(f"/leagues/{lid}/draft-order", json={"mode": "manual", "team_order": new_order}, headers=HDR["commish"])
check(resp.status_code == 200 and resp.json()["draft_order_mode"] == "manual", resp.text)
check([t["id"] for t in sorted(teams_of(lid), key=lambda t: t["draft_position"])] == new_order, "the new order is saved")

# random -> manual queues the settings email; reordering inside manual changes no watched setting, so it queues none
queued = db.query(EmailOutbox).count()
resp = client.put(f"/leagues/{default_league['id']}/draft-order", json={"mode": "manual", "team_order": [t["id"] for t in teams_of(default_league["id"])]},
                  headers=HDR["commish"])
check(resp.status_code == 200, resp.text)
db.expire_all()
check(db.query(EmailOutbox).count() > queued, "changing the mode queues a settings email")
queued = db.query(EmailOutbox).count()
client.put(f"/leagues/{lid}/draft-order", json={"mode": "manual", "team_order": ids}, headers=HDR["commish"])
client.put(f"/leagues/{lid}/draft-order", json={"mode": "manual", "team_order": new_order}, headers=HDR["commish"])
db.expire_all()
check(db.query(EmailOutbox).count() == queued, "reordering inside manual mode sends no email")
resp = client.put(f"/leagues/{default_league['id']}/draft-order", json={"mode": "random"}, headers=HDR["commish"])
check(resp.status_code == 200 and all(t["draft_position"] is None for t in teams_of(default_league["id"])),
      "switching back to random clears the positions")

# --- starting the draft ----------------------------------------------------------------------------------------------
bogus = list(reversed(new_order))      # the draft screen's own order must lose to the saved one
resp = client.post("/drafts/start", json={"league_id": lid, "randomize_order": True, "team_order": bogus}, headers=HDR["commish"])
check(resp.status_code == 200, resp.text)
draft_id = resp.json()["id"]
check(first_round(draft_id) == new_order, f"round 1 follows the saved order: {first_round(draft_id)} vs {new_order}")
check(second_round(draft_id) == list(reversed(new_order)), "a snake draft reverses the order in round 2")

# --- locked once started -------------------------------------------------------------------------------------------------
resp = client.put(f"/leagues/{lid}/draft-order", json={"mode": "manual", "team_order": ids}, headers=HDR["commish"])
check(resp.status_code == 400, "the order can't change after the draft starts")
resp = client.put(f"/leagues/{lid}/draft-order", json={"mode": "random"}, headers=HDR["commish"])
check(resp.status_code == 400, "nor can it go back to random")

# --- a random league still takes the draft screen's order ---------------------------------------------------------------
rid = default_league["id"]
rids = [t["id"] for t in teams_of(rid)]
screen = [rids[2], rids[0], rids[3], rids[1]]
resp = client.post("/drafts/start", json={"league_id": rid, "randomize_order": False, "team_order": screen}, headers=HDR["commish"])
check(resp.status_code == 200 and first_round(resp.json()["id"]) == screen, "random mode keeps the draft screen's order")

print(f"draft order smoke test: {checks} checks passed")
db.close()
engine.dispose()
if db_path.exists():
    db_path.unlink()
