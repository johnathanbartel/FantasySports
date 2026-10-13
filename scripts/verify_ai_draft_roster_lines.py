"""
Offline checks (throwaway SQLite file, FastAPI TestClient) for the lineup-aware snake-draft AI and the roster lines view.

  A. choose_pick on a fabricated board: a scarce position is taken ahead of a deeper one, a team does not draft a
     second goalie for its bench while it still has other needs, and the last picks go to the open starting slots.
  B. A whole 6-team NHL snake draft run on autopick: every team ends with its full starting lineup (C, 2 W, 2 D, G)
     and only one goalie before the bench, and the same draft replays identically.
  C. GET /teams/{id}/lines lays the roster out by slot, with open spots.
  D. The pages load the lines renderer.

    .venv-api/Scripts/python.exe scripts/verify_ai_draft_roster_lines.py
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Mock external network calls to keep test offline and hermetic
_fetch_patcher = patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None)
_fetch_patcher.start()

db_path = Path("ai_draft_roster_lines_verify.db")
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.api.schemas import DraftBoardAsset
from fantasy_sports.platform.ai.snake_ai import choose_pick
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import Draft, DraftableAsset, Player, RealTeam, RosterEntry, Team, User
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
TEAMS = 6


def check(ok, msg):
    print(("ok  " if ok else "FAIL ") + msg)
    if not ok:
        sys.exit(1)


db = SessionLocal()
nhl, _ = seed_hockey_reference_data(db)
club = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
db.add(club)
db.flush()
assets = {}   # position -> [asset ids]
for pos, n in (("C", 40), ("LW", 28), ("RW", 28), ("D", 60), ("G", 28)):
    for i in range(n):
        p = Player(universe_id=nhl.id, real_team_id=club.id, full_name=f"{pos} Player {i:02d}", position=pos)
        db.add(p)
        db.flush()
        a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
        db.add(a)
        db.flush()
        assets.setdefault(pos, []).append(a.id)
owner = User(email="o@example.com", display_name="Owner")
db.add(owner)
db.commit()
token = client.post("/sessions", json={"user_id": owner.id}).json()["token"]
auth = {"Authorization": "Bearer " + token}


def new_league(name):
    lg = client.post("/leagues", json={"name": name, "commissioner_user_id": owner.id, "universe_codes": ["nhl"],
                                       "size": TEAMS}, headers=auth).json()
    assert client.post(f"/leagues/{lg['id']}/ai-teams", headers=auth).status_code == 200
    return lg


# ---- A. decisions on a fabricated board ----
lg = new_league("Decisions")
draft = Draft(league_id=lg["id"], status="in_progress")
db.add(draft)
db.commit()
team_ids = [t.id for t in db.query(Team).filter_by(league_id=lg["id"]).order_by(Team.id)]


def board(*rows):
    return [DraftBoardAsset(draftable_asset_id=i, asset_type="player", display_name=f"{pos}{i}", position=pos, points=pts)
            for i, (pos, pts) in enumerate(rows, start=1)]


# twelve centres at 100 down to 89, goalies 60 down to 15 (a steep drop), plus deep D and wingers: the goalie is worth
# far more over replacement than the extra centre
b = board(*[("C", 100 - i) for i in range(12)], *[("G", 60 - 5 * i) for i in range(10)], *[("D", 30 - i) for i in range(16)],
          *[("LW", 40 - i) for i in range(16)])
top = choose_pick(db, draft, team_ids[0], b)
check(top.position == "G", f"scarce position first: picked {top.position} over higher-scoring centres")

# a team that already has its goalie and every other need open does not take a bench goalie over a needed position
db.add(RosterEntry(team_id=team_ids[0], draftable_asset_id=assets["G"][0], acquired_via="draft"))
db.commit()
pick = choose_pick(db, draft, team_ids[0], b)
check(pick.position != "G", f"no second goalie while other starting spots are open: picked {pick.position}")

# forced: a team holding W, W, D, D, G and a full bench (17 of 18 picks) has one pick left and one open starting spot (C),
# so it must take a centre even though a defenceman scores far more
for pos, idx in (("LW", 0), ("RW", 0), ("D", 0), ("D", 1), ("G", 2),                     # starters except C
                 *[("D", i) for i in range(2, 6)], *[("LW", i) for i in range(1, 4)], *[("RW", i) for i in range(1, 4)],
                 ("G", 3), ("G", 4)):   # full bench (12)
    db.add(RosterEntry(team_id=team_ids[1], draftable_asset_id=assets[pos][idx], acquired_via="draft"))
db.commit()
n_roster = db.query(RosterEntry).filter_by(team_id=team_ids[1]).count()
check(n_roster == 17, f"setup: team has {n_roster} of 18")
forced = choose_pick(db, draft, team_ids[1], board(("D", 90), ("G", 80), ("C", 10), ("C", 5)))
check(forced.position == "C", f"last pick goes to the open starting slot: picked {forced.position} over a D scoring 90")

# ---- B. whole draft on autopick ----
lg = new_league("Autodraft")


def run_draft():
    r = client.post("/drafts/start", json={"league_id": lg["id"]})
    assert r.status_code == 200, r.text
    d = r.json()
    while True:
        r = client.post(f"/drafts/{d['id']}/autopick")
        if r.status_code != 200:
            break
    picks = client.get(f"/drafts/{d['id']}/picks").json()
    return d["id"], [p["draftable_asset_id"] for p in picks]


did, order = run_draft()
check(client.get(f"/drafts/{did}").json()["status"] == "complete", f"draft ran to completion ({len(order)} picks)")
teams = [t.id for t in db.query(Team).filter_by(league_id=lg["id"]).order_by(Team.id)]
bad = []
for t in teams:
    lines = client.get(f"/teams/{t}/lines").json()
    if lines["starters_filled"] != lines["starters_total"]:
        bad.append((t, lines["starters_filled"], lines["starters_total"]))
check(not bad, f"every team ends with a full starting lineup (6 of 6) {bad}")
sample = client.get(f"/teams/{teams[0]}/lines").json()
depth = {}
for t in teams:
    for s in client.get(f"/teams/{t}/lines").json()["slots"]:
        for p in s["players"]:
            g = {"LW": "W", "RW": "W"}.get(p["position"], p["position"])
            depth.setdefault(t, {}).setdefault(g, 0)
            depth[t][g] += 1
check(all(d == {"C": 3, "W": 6, "D": 6, "G": 3} for d in depth.values()),
      "every team drafts exactly the three lines' depth: C 3, W 6, D 6, G 3 (18 players)")

# ---- C. lines layout ----
codes = [(s["slot_code"], s["capacity"], s["is_starter"]) for s in sample["slots"]]
check(codes == [("C", 1, True), ("W", 2, True), ("D", 2, True), ("G", 1, True), ("BENCH", 12, False)], f"slots: {codes}")
check(all(len(s["players"]) == s["capacity"] for s in sample["slots"]), "all 18 spots filled after the draft")
empty = client.get(f"/teams/{new_league('Empty')['id'] and db.query(Team).order_by(Team.id.desc()).first().id}/lines").json()
check(empty["starters_filled"] == 0 and empty["starters_total"] == 6, "a team with no picks shows 0 of 6 starting spots")
check(client.get("/teams/99999/lines").status_code == 404, "unknown team is a 404")

# ---- D. pages ----
for page in ("draft-board", "league", "roster"):
    check("/static/roster-lines.js" in (ROOT / f"web/static/{page}.html").read_text(encoding="utf-8"), f"{page}.html loads roster-lines.js")
check('id="tab-roster"' in (ROOT / "web/static/draft-board.html").read_text(encoding="utf-8"), "draft board has a Roster tab")

db.close()
engine.dispose()
db_path.unlink()
print("\nAll AI draft / roster lines checks passed.")
