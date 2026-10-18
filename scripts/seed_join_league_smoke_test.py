"""
Join League through the real HTTP API (FastAPI TestClient) on a throwaway SQLite file (join_league_smoke_test.db):
public/private visibility, open human slots, taking over AI teams, the draft-in-progress and in-season rules, the one-hour
cutoff with its two policies (fill with AI / remove), and the public league list.

    .venv-api/Scripts/python.exe scripts/seed_join_league_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("JOIN_SMOKE_DB", "join_league_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import DraftableAsset, Draft, Player, RealTeam, User
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
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "a", "b", "c", "d")}
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


def make_league(name, visibility="public", policy="fill_ai", takeover=False, slots=1, draft_in=None, size=4):
    """size teams: the commissioner's, `slots` open human slots, the rest AI."""
    body = {"name": name, "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"], "size": size,
            "visibility": visibility, "unfilled_slot_policy": policy, "allow_ai_takeover_in_season": takeover}
    if draft_in is not None:
        body["draft_scheduled_at"] = (datetime.datetime.utcnow() + draft_in).isoformat()
    resp = client.post("/leagues", json=body, headers=HDR["commish"])
    assert resp.status_code == 200, resp.text
    league = resp.json()
    items = [{"name": "Commish FC", "is_commissioner": True}]
    items += [{"name": f"Open {i}", "is_open_slot": True} for i in range(slots)]
    items += [{"name": f"AI {i}", "is_ai": True} for i in range(size - 1 - slots)]
    resp = client.post(f"/leagues/{league['id']}/teams-setup", json={"teams": items}, headers=HDR["commish"])
    assert resp.status_code == 200, resp.text
    return league, resp.json()["teams"]


def options(lid, who=None):
    resp = client.get(f"/leagues/{lid}/join-options", headers=HDR[who] if who else {})
    assert resp.status_code == 200, resp.text
    return resp.json()


def join(lid, who, team_id, name=None):
    return client.post(f"/leagues/{lid}/join", json={"team_id": team_id, "team_name": name}, headers=HDR[who])


# --- creation and settings --------------------------------------------------------------------------------------------
league, teams = make_league("Public One", slots=1)
check(league["visibility"] == "public" and league["unfilled_slot_policy"] == "fill_ai", "league settings are stored")
check(sum(t["open_human_slot"] for t in teams) == 1, "one open human slot was created")
resp = client.post("/leagues", headers=HDR["commish"], json={
    "name": "Bad", "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"], "size": 4,
    "visibility": "private", "allow_ai_takeover_in_season": True})
check(resp.status_code == 400, "in-season takeover needs a public league")
resp = client.post("/leagues", headers=HDR["commish"], json={
    "name": "Bad", "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"], "visibility": "secret"})
check(resp.status_code == 422, "visibility must be public or private")

# --- public league: open slot and AI takeover before the draft --------------------------------------------------------
opts = options(league["id"], "a")
kinds = {o["kind"] for o in opts["teams"]}
check(opts["can_join"] and kinds == {"open_slot", "ai_takeover"}, f"open slot and AI takeover offered: {opts}")
check(options(league["id"])["can_join"], "options are readable without signing in")
open_id = next(t["id"] for t in teams if t["open_human_slot"])
ai_id = next(t["id"] for t in teams if t["is_ai_owned"] and not t["open_human_slot"])
check(client.post(f"/leagues/{league['id']}/join", json={"team_id": open_id}).status_code == 401, "joining needs sign-in")
resp = join(league["id"], "a", open_id, "Alpha Wolves")
check(resp.status_code == 200 and resp.json()["owner_user_id"] == users["a"].id, resp.text)
check(resp.json()["name"] == "Alpha Wolves" and not resp.json()["is_ai_owned"] and not resp.json()["open_human_slot"],
      "the slot became a human team with the chosen name")
check(join(league["id"], "a", ai_id).status_code == 409, "one team per person per league")
check(join(league["id"], "b", open_id).status_code == 409, "a claimed slot can't be claimed again")
resp = join(league["id"], "b", ai_id)
check(resp.status_code == 200 and not resp.json()["is_ai_owned"], "an AI team can be taken over before the draft")
check(join(league["id"], "d", ai_id).status_code == 409, "a person-managed team can't be taken over")

# --- private league: slots yes, AI takeover no ------------------------------------------------------------------------
priv, pteams = make_league("Private One", visibility="private", slots=1)
pops = options(priv["id"], "a")
check({o["kind"] for o in pops["teams"]} == {"open_slot"}, "a private league offers its open slot only")
pai = next(t["id"] for t in pteams if t["is_ai_owned"] and not t["open_human_slot"])
check(join(priv["id"], "a", pai).status_code == 409, "AI teams in a private league can't be taken over")
check(join(priv["id"], "a", next(t["id"] for t in pteams if t["open_human_slot"])).status_code == 200,
      "a private open slot is claimable")
pub_ids = {l["id"] for l in client.get("/leagues/public").json()}
check(priv["id"] not in pub_ids, "a private league isn't listed")
check(league["id"] in pub_ids, "a public league with an AI team left is still listed")
free, _ = make_league("Listed", slots=1)
listed = {l["id"]: l for l in client.get("/leagues/public").json()}
check(free["id"] in listed and listed[free["id"]]["open_slots"] == 1 and listed[free["id"]]["ai_takeovers"] == 2,
      f"listed with its counts: {listed}")

# --- draft in progress, then in season --------------------------------------------------------------------------------
live, lteams = make_league("Live", slots=0)
d = Draft(league_id=live["id"], status="in_progress")
db.add(d)
db.commit()
o = options(live["id"], "a")
check(not o["can_join"] and "in progress" in o["reason"], f"no joining mid-draft: {o}")
d.status = "complete"
db.commit()
o = options(live["id"], "a")
check(not o["can_join"] and "doesn't allow" in o["reason"], f"season begun, takeover off: {o}")
seasonal, steams = make_league("Seasonal", takeover=True, slots=0)
db.add(Draft(league_id=seasonal["id"], status="complete"))
db.commit()
o = options(seasonal["id"], "a")
check(o["can_join"] and {x["kind"] for x in o["teams"]} == {"ai_takeover"}, f"in-season takeover allowed: {o}")
check(join(seasonal["id"], "a", o["teams"][0]["id"]).status_code == 200, "took over an AI team in season")
resp = client.patch(f"/leagues/{live['id']}/settings", json={"allow_ai_takeover_in_season": True}, headers=HDR["commish"])
check(resp.status_code == 200 and options(live["id"], "a")["can_join"], "the commissioner can allow in-season takeover later")
resp = client.patch(f"/leagues/{priv['id']}/settings", json={"allow_ai_takeover_in_season": True}, headers=HDR["commish"])
check(resp.status_code == 400, "can't allow in-season takeover on a private league")

# --- the one-hour cutoff ----------------------------------------------------------------------------------------------
soon, _ = make_league("Soon Fill", policy="fill_ai", slots=2, draft_in=datetime.timedelta(minutes=30))
resp = client.get(f"/leagues/{soon['id']}/teams")
check(resp.status_code == 200 and not any(t["open_human_slot"] for t in resp.json()), "past the cutoff, slots turn into AI teams")
check(client.get(f"/leagues/{soon['id']}").json()["size"] == 4 and len(resp.json()) == 4, "fill_ai keeps the league size")

gone, _ = make_league("Soon Remove", policy="remove", slots=2, draft_in=datetime.timedelta(minutes=30))
check(client.get(f"/leagues/{gone['id']}").json()["size"] == 2, "remove shrinks the league")
check(len(client.get(f"/leagues/{gone['id']}/teams").json()) == 2, "unclaimed slots were deleted")

later, _ = make_league("Later", policy="remove", slots=2, draft_in=datetime.timedelta(hours=3))
check(client.get(f"/leagues/{later['id']}").json()["size"] == 4, "before the cutoff nothing changes")
lt = client.get(f"/leagues/{later['id']}/teams").json()
check(sum(t["open_human_slot"] for t in lt) == 2, "slots stay open before the cutoff")
check(join(later["id"], "c", next(t["id"] for t in lt if t["open_human_slot"])).status_code == 200,
      "a slot can be claimed before the cutoff")

# starting the draft early resolves unclaimed slots at once
tiny, _ = make_league("Tiny", policy="remove", slots=1, size=2)
resp = client.post("/drafts/start", json={"league_id": tiny["id"]}, headers=HDR["commish"])
check(client.get(f"/leagues/{tiny['id']}").json()["size"] == 2, "a league never shrinks below 2 teams")
early, _ = make_league("Early AI", policy="fill_ai", slots=1)
resp = client.post("/drafts/start", json={"league_id": early["id"]}, headers=HDR["commish"])
check(resp.status_code == 200, resp.text)
check(not any(t["open_human_slot"] for t in client.get(f"/leagues/{early['id']}/teams").json()),
      "starting the draft resolves open slots")
early_rm, _ = make_league("Early Remove", policy="remove", slots=1)
resp = client.post("/drafts/start", json={"league_id": early_rm["id"]}, headers=HDR["commish"])
check(resp.status_code == 200 and client.get(f"/leagues/{early_rm['id']}").json()["size"] == 3,
      "starting the draft removes unclaimed slots under the remove policy")

print(f"join league smoke test passed ({checks} checks)")
