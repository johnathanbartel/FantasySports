"""
Offline checks (throwaway SQLite file, FastAPI TestClient) for:
  * a draft cannot start while the league has fewer teams than its size (snake and auction), and can once the
    commissioner fills the open spots with AI teams
  * GET /leagues/mine/dashboard lists the caller's leagues across sports, with fill count and draft state
  * the draft board shows a line for an untimed draft, and the dashboard page is wired to its endpoint

    .venv-api/Scripts/python.exe scripts/verify_league_fill_dashboard.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
db_path = Path("league_fill_dashboard_verify.db")
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import DraftableAsset, Player, RealTeam, Team, User
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.platform.nfl_reference import seed_nfl_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]


def check(ok, msg):
    print(("ok  " if ok else "FAIL ") + msg)
    if not ok:
        sys.exit(1)


db = SessionLocal()
nhl, _ = seed_hockey_reference_data(db)
seed_nfl_reference_data(db)
team = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
db.add(team)
db.flush()
for i in range(70):   # a 4-team NHL draft fills 4 x 14 roster spots
    p = Player(universe_id=nhl.id, real_team_id=team.id, full_name=f"Skater {i}", position="C")
    db.add(p)
    db.flush()
    db.add(DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id))
alice = User(email="a@example.com", display_name="Alice")
bob = User(email="b@example.com", display_name="Bob")
db.add_all([alice, bob])
db.commit()


def auth(user):
    return {"Authorization": "Bearer " + client.post("/sessions", json={"user_id": user.id}).json()["token"]}


a, b = auth(alice), auth(bob)


def make_league(name, codes, size):
    r = client.post("/leagues", json={"name": name, "commissioner_user_id": alice.id, "universe_codes": codes,
                                      "size": size}, headers=a)
    assert r.status_code == 200, r.text
    return r.json()


# ---- fill guard ----
hockey = make_league("Hockey League", ["nhl"], 4)
db.add(Team(league_id=hockey["id"], name="Alice's Team", owner_user_id=alice.id, program_size="large"))
db.commit()

r = client.post("/drafts/start", json={"league_id": hockey["id"]})
check(r.status_code == 400 and "isn't full" in r.json()["detail"], f"snake draft refused on an unfilled league ({r.json().get('detail')})")
r = client.post("/auctions/start", json={"league_id": hockey["id"], "round_seconds": 30}, headers=a)
check(r.status_code == 400 and "isn't full" in r.json()["detail"], "auction refused on an unfilled league")
r = client.post("/recruiting/start", json={"league_id": hockey["id"]}, headers=a)
check(r.status_code == 400 and "isn't full" in r.json()["detail"], "recruiting draft refused on an unfilled league")

# ---- dashboard (before filling) ----
football = make_league("Football League", ["nfl"], 2)
db.add(Team(league_id=football["id"], name="Bob's Team", owner_user_id=bob.id, program_size="small"))
db.commit()
rows = {x["name"]: x for x in client.get("/leagues/mine/dashboard", headers=a).json()}
check(set(rows) == {"Hockey League", "Football League"}, "commissioner sees leagues from both sports")
h = rows["Hockey League"]
check(h["sports"] == ["nhl"] and h["teams_filled"] == 1 and h["size"] == 4 and h["is_commissioner"]
      and h["draft_state"] == "not_started" and h["my_team_names"] == ["Alice's Team"], "hockey row: sport, fill, role, team, draft state")
rows_b = {x["name"]: x for x in client.get("/leagues/mine/dashboard", headers=b).json()}
check(set(rows_b) == {"Football League"} and not rows_b["Football League"]["is_commissioner"]
      and rows_b["Football League"]["my_team_names"] == ["Bob's Team"], "a manager sees only their own league")
check(client.get("/leagues/mine/dashboard").status_code == 401, "dashboard requires sign-in")

# ---- fill, then start ----
r = client.post(f"/leagues/{hockey['id']}/ai-teams", headers=b)
check(r.status_code == 403, "only the commissioner can add AI teams")
r = client.post(f"/leagues/{hockey['id']}/ai-teams", headers=a)
check(r.status_code == 200 and len(r.json()) == 3, "commissioner fills the 3 open spots with AI teams")
r = client.post("/drafts/start", json={"league_id": hockey["id"], "pick_seconds": 60}, headers=a)
check(r.status_code == 200, "snake draft starts once the league is full")
rows = {x["name"]: x for x in client.get("/leagues/mine/dashboard", headers=a).json()}
check(rows["Hockey League"]["teams_filled"] == 4 and rows["Hockey League"]["draft_state"] == "in_progress",
      "dashboard shows the league full and its draft in progress")

# ---- pages ----
board = (ROOT / "web/static/draft-board.html").read_text(encoding="utf-8")
check("No pick clock - this draft was started without one" in board, "draft board explains an untimed draft")
check('<option value="live" selected>' in board, "clock setup defaults to a live clock")
dash = (ROOT / "web/static/dashboard.html").read_text(encoding="utf-8")
check("/leagues/mine/dashboard" in dash and "dashboard.html" in (ROOT / "web/static/index.html").read_text(encoding="utf-8"),
      "dashboard page calls the endpoint and is linked from the home page")

db.close()
engine.dispose()
db_path.unlink()
print("\nAll league fill / dashboard checks passed.")
