"""
Mock drafts through the real HTTP API (FastAPI TestClient) on a throwaway SQLite file (mock_draft_smoke_test.db): creating
public and private mock drafts with human and AI slots, the validation, the public list, joining and leaving, the start
time starting the draft by itself with unclaimed human slots either filled with AI or removed, starting early, cancelling,
and that mock drafts stay out of the ordinary league lists.

    .venv-api/Scripts/python.exe scripts/seed_mock_draft_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("MOCK_DRAFT_SMOKE_DB", "mock_draft_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import Draft, DraftableAsset, DraftPick, League, Player, RealTeam, Team, User
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
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("host", "a", "b", "c", "d")}
db.add_all(users.values())
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


HDR = {n: login(u) for n, u in users.items()}
checks = 0
ROUNDS = 18          # hockey: one round per roster spot (platform/hockey_reference.py)


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


def in_hours(h):
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=h)).isoformat()


def make(name="Mock", size=6, humans=3, visibility="public", fill=True, start=None, who="host", **extra):
    body = {"name": name, "universe_code": "nhl", "size": size, "human_slots": humans, "start_at": start or in_hours(2),
            "visibility": visibility, "fill_ai_if_unfilled": fill, **extra}
    return client.post("/mock-drafts", json=body, headers=HDR[who])


def get(mid, who=None):
    return client.get(f"/mock-drafts/{mid}", headers=HDR[who] if who else {})


def kinds(m):
    return sorted(t["kind"] for t in m["teams"])


def set_start(mid, when):
    with SessionLocal() as s:
        s.get(League, mid).draft_scheduled_at = when
        s.commit()


# ---- Creating -------------------------------------------------------------------------------------------------------
r = make()
check(r.status_code == 200, r.text)
pub = r.json()
check(pub["status"] == "lobby" and pub["visibility"] == "public" and pub["size"] == 6, "created in the lobby")
check(kinds(pub) == ["ai", "ai", "ai", "human", "open", "open"], f"slots: {kinds(pub)}")
check(pub["humans_joined"] == 1 and pub["human_slots"] == 3 and pub["open_slots"] == 2, "human slot counts")
check(pub["is_creator"] and pub["my_team_id"] is not None and pub["fill_ai_if_unfilled"], "creator is in, flag kept")
check(pub["rounds"] == ROUNDS and pub["pick_seconds"] == 60, "hockey drafts a round per roster spot; default pick clock")

check(client.post("/mock-drafts", json={}).status_code in (401, 422), "creating needs an account")
check(make(start=in_hours(-3)).status_code == 400, "a start time in the past is refused")
check(make(start=in_hours(24 * 90)).status_code == 400, "a start time months away is refused")
check(make(size=1, humans=1).status_code == 422, "one team isn't a draft")
check(make(size=4, humans=5).status_code == 400, "more human slots than teams is refused")
check(make(size=12, humans=2).status_code == 400, "a draft bigger than the player pool is refused")
check(client.post("/mock-drafts", json={"name": "x", "universe_code": "cricket", "size": 4, "human_slots": 1,
                                        "start_at": in_hours(1)}, headers=HDR["host"]).status_code == 400, "unknown sport")
with SessionLocal() as s:
    check(s.query(League).filter(League.is_mock_draft.is_(True)).count() == 1, "refused requests left nothing behind")

# ---- Staying out of the league lists --------------------------------------------------------------------------------
check(client.get("/leagues/mine", headers=HDR["host"]).json() == [], "not in My leagues")
check(client.get("/leagues/mine/dashboard", headers=HDR["host"]).json() == [], "not on the dashboard")
check(client.get("/leagues/public").json() == [], "not on the Join League page")

# ---- Public list, joining and leaving -------------------------------------------------------------------------------
priv = make(name="Private", visibility="private").json()
listed = client.get("/mock-drafts/public").json()
check([m["id"] for m in listed] == [pub["id"]], "only the public mock draft is listed, no sign-in needed")
check(get(pub["id"]).json()["can_join"], "an anonymous visitor sees it's joinable")

check(client.post(f"/mock-drafts/{pub['id']}/join").status_code == 401, "joining needs an account")
j = client.post(f"/mock-drafts/{pub['id']}/join", json={"team_name": "Team A"}, headers=HDR["a"])
check(j.status_code == 200 and j.json()["my_team_id"] and j.json()["open_slots"] == 1, j.text)
check(any(t["name"] == "Team A" and t["is_you"] for t in j.json()["teams"]), "joined under the name given")
check(client.post(f"/mock-drafts/{pub['id']}/join", headers=HDR["a"]).status_code == 409, "can't take two slots")
check(client.post(f"/mock-drafts/{pub['id']}/join", headers=HDR["host"]).status_code == 409, "the creator already has one")
check(client.post(f"/mock-drafts/{pub['id']}/join", headers=HDR["b"]).status_code == 200, "second person joins")
full = client.post(f"/mock-drafts/{pub['id']}/join", headers=HDR["c"])
check(full.status_code == 409, "no human slot left")
check(client.get("/mock-drafts/public").json() == [], "a full mock draft drops off the public list")

lv = client.post(f"/mock-drafts/{pub['id']}/leave", headers=HDR["b"])
check(lv.status_code == 200 and lv.json()["open_slots"] == 1 and lv.json()["my_team_id"] is None, lv.text)
check(client.post(f"/mock-drafts/{pub['id']}/leave", headers=HDR["host"]).status_code == 409, "the creator can't leave")
check(client.post(f"/mock-drafts/{pub['id']}/leave", headers=HDR["c"]).status_code == 409, "a stranger can't leave")
check(client.post(f"/mock-drafts/{pub['id']}/join", headers=HDR["c"]).status_code == 200, "the freed slot can be taken")

check(client.post(f"/mock-drafts/{priv['id']}/join", headers=HDR["a"]).status_code == 200, "a private draft is joined by its link")
mine = client.get("/mock-drafts/mine", headers=HDR["a"]).json()
check({m["id"] for m in mine} == {pub["id"], priv["id"]}, "My mock drafts lists both")

# ---- The start time starts the draft; AI fills unclaimed human slots ------------------------------------------------
check(get(pub["id"]).json()["status"] == "lobby", "still waiting before the start time")
set_start(pub["id"], datetime.datetime.utcnow() - datetime.timedelta(seconds=5))
started = get(pub["id"], "a").json()
check(started["status"] == "in_progress", "reading the lobby after the start time starts the draft")
check(started["size"] == 6 and kinds(started) == ["ai", "ai", "ai", "human", "human", "human"], f"full draft: {kinds(started)}")
with SessionLocal() as s:
    d = s.query(Draft).filter_by(league_id=pub["id"]).one()
    check(d.status == "in_progress" and d.pick_seconds == 60, "timed draft with the creator's pick clock")
    check(s.query(DraftPick).filter_by(draft_id=d.id).count() == 6 * ROUNDS, "a pick for every team in every round")
check(client.post(f"/mock-drafts/{pub['id']}/join", headers=HDR["d"]).status_code == 409, "no joining once it has started")
check(get(pub["id"]).json()["status"] == "in_progress", "reading again doesn't start a second draft")
with SessionLocal() as s:
    check(s.query(Draft).filter_by(league_id=pub["id"]).count() == 1, "exactly one draft")

# ---- Unclaimed slots: filled with AI, or removed --------------------------------------------------------------------
fill = make(name="Fill AI", size=6, humans=4, fill=True).json()
rem = make(name="Remove", size=6, humans=4, fill=False).json()
for m in (fill, rem):
    client.post(f"/mock-drafts/{m['id']}/join", headers=HDR["a"])
    set_start(m["id"], datetime.datetime.utcnow() - datetime.timedelta(seconds=1))
f = get(fill["id"]).json()
check(f["status"] == "in_progress" and f["size"] == 6 and kinds(f) == ["ai"] * 4 + ["human"] * 2, f"AI took the empty slots: {kinds(f)}")
r2 = get(rem["id"]).json()
check(r2["status"] == "in_progress" and r2["size"] == 4 and kinds(r2) == ["ai", "ai", "human", "human"],
      f"empty slots removed: {kinds(r2)}")
with SessionLocal() as s:
    d = s.query(Draft).filter_by(league_id=rem["id"]).one()
    check(s.query(DraftPick).filter_by(draft_id=d.id).count() == 4 * ROUNDS, "the shrunken draft has picks for 4 teams only")

# ---- Starting early, and who may ----------------------------------------------------------------------------------
early = make(name="Early", size=4, humans=2).json()
check(client.post(f"/mock-drafts/{early['id']}/start", headers=HDR["a"]).status_code == 403, "only the creator starts early")
check(client.post(f"/mock-drafts/{early['id']}/start", headers=HDR["host"]).json()["status"] == "in_progress", "creator starts early")
check(client.post(f"/mock-drafts/{early['id']}/start", headers=HDR["host"]).status_code == 409, "can't start twice")

# ---- Cancelling ---------------------------------------------------------------------------------------------------
check(client.delete(f"/mock-drafts/{priv['id']}", headers=HDR["a"]).status_code == 403, "only the creator cancels")
check(client.delete(f"/mock-drafts/{priv['id']}", headers=HDR["host"]).status_code == 200, "creator cancels")
check(get(priv["id"]).status_code == 404, "gone")
check(client.delete(f"/mock-drafts/{early['id']}", headers=HDR["host"]).status_code == 200, "a started draft can be deleted too")

# ---- A real league isn't a mock draft; finishing a mock draft leaves no season behind -----------------------------
with SessionLocal() as s:
    lg = League(name="Real", commissioner_user_id=users["host"].id, size=4)
    s.add(lg)
    s.commit()
    real_id = lg.id
check(get(real_id).status_code == 404, "ordinary leagues aren't reachable as mock drafts")

tiny = make(name="Tiny", size=2, humans=1).json()
client.post(f"/mock-drafts/{tiny['id']}/start", headers=HDR["host"])
with SessionLocal() as s:
    draft_id = s.query(Draft).filter_by(league_id=tiny["id"]).one().id
for _ in range(2 * ROUNDS + 2):
    with SessionLocal() as s2:         # let the pick clock run out so the server picks for whoever is on it
        d = s2.get(Draft, draft_id)
        d.deadline_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        s2.commit()
    r = client.post(f"/drafts/{draft_id}/process-overdue", headers=HDR["host"])
    check(r.status_code == 200, r.text)
check(get(tiny["id"]).json()["status"] == "complete", "the mock draft completes")
with SessionLocal() as s:
    from fantasy_sports.platform.db.models.core import WeeklyMatchup
    check(s.query(WeeklyMatchup).filter_by(league_id=tiny["id"]).count() == 0, "no schedule is built for a mock draft")
    check(s.query(Team).filter_by(league_id=tiny["id"]).count() == 2, "teams kept for the results")

print(f"mock draft smoke test passed ({checks} checks)")
