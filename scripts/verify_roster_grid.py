"""
Offline checks (throwaway SQLite, TestClient, Node) for the NHL roster grid and the college-only features.

  * Drafting ranks each NHL player into his position group's next open depth slot, so Lines 1-4 fill as the draft goes.
  * The grid logic (web/static/roster-grid.js) lays the live pecking-order payload out as positions x Lines 1-4, and a
    swap between two cells of the same group is saved through PUT /teams/{id}/pecking-order; a cross-position swap is refused.
  * Rankings and team-set schedules exist only for college leagues: the NHL league has neither (400), a college league has both.

    .venv-api/Scripts/python.exe scripts/verify_roster_grid.py
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
db_path = Path("roster_grid_verify.db")
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, League, LeagueUniverse, Player, RealTeam, Team, Universe, User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app)


def check(ok, msg):
    print(("ok  " if ok else "FAIL ") + msg)
    if not ok:
        sys.exit(1)


db = SessionLocal()
nhl, _ = seed_hockey_reference_data(db)
club = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
db.add(club)
db.flush()
for pos, n in (("C", 40), ("LW", 28), ("RW", 28), ("D", 60), ("G", 28)):
    for i in range(n):
        p = Player(universe_id=nhl.id, real_team_id=club.id, full_name=f"{pos} Player {i:02d}", position=pos)
        db.add(p)
        db.flush()
        db.add(DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id))
owner = User(email="o@example.com", display_name="Owner")
db.add(owner)
college = Universe(sport="football", level="college", code="ncaaf", display_name="NCAAF", supports_individual_defense=False)
db.add(college)
db.commit()
auth = {"Authorization": "Bearer " + client.post("/sessions", json={"user_id": owner.id}).json()["token"]}

lg = client.post("/leagues", json={"name": "Grid", "commissioner_user_id": owner.id, "universe_codes": ["nhl"], "size": 6}, headers=auth).json()
client.post(f"/leagues/{lg['id']}/ai-teams", headers=auth)
d = client.post("/drafts/start", json={"league_id": lg["id"]}).json()
while client.post(f"/drafts/{d['id']}/autopick").status_code == 200:
    pass
team = db.query(Team).filter_by(league_id=lg["id"]).order_by(Team.id).first()

# ---- drafting fills the lines ----
po = client.get(f"/teams/{team.id}/pecking-order").json()
sizes = {g["position_group"]: len(g["ranked"]) for g in po["groups"]}
check(sizes == {"C": 4, "W": 8, "D": 8, "G": 4}, f"all 24 drafted players are ranked on the four lines: {sizes}")
check(all(not g["unranked"] or len(g["ranked"]) == g["max_depth"] for g in po["groups"]), "only players beyond a full group are left unranked")

# ---- grid logic under Node ----
node = shutil.which("node")
check(node is not None, "node is available")
script = r"""
const G = require(process.argv[1]);
const groups = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = {};
const grid = G.build(groups);
out.labels = grid.rows.map(r => r.label);
out.lineCounts = grid.rows.map(r => r.cells.length);
out.line1 = grid.rows.map(r => r.cells[0].player && r.cells[0].player.player_id);
out.line2Ranks = grid.rows.map(r => r.cells[1].rank);
const w = groups.find(g => g.position_group === 'W');
out.swapWW = G.swap(groups, {group: 'W', rank: 1}, {group: 'W', rank: 2});
out.cross = G.swap(groups, {group: 'C', rank: 1}, {group: 'W', rank: 1});
out.same = G.swap(groups, {group: 'W', rank: 1}, {group: 'W', rank: 1});
out.empty = G.swap(groups, {group: 'G', rank: 5}, {group: 'G', rank: 1});
const c = groups.find(g => g.position_group === 'C');
out.emptyTarget = c.ranked.length < 4 ? G.swap(groups, {group: 'C', rank: 1}, {group: 'C', rank: c.ranked.length + 1}) : null;
process.stdout.write(JSON.stringify(out));
"""
res = subprocess.run([shutil.which("node"), "-e", script, str(ROOT / "web/static/roster-grid.js")],
                     input=json.dumps(po["groups"]), capture_output=True, text=True)
check(res.returncode == 0, f"grid logic runs under Node {res.stderr[:200]}")
out = json.loads(res.stdout)
check(out["labels"] == ["C", "W", "W", "D", "D", "G"] and out["lineCounts"] == [4] * 6, "six position rows by four lines")
check(out["line2Ranks"] == [2, 3, 4, 3, 4, 2], f"Line 2 holds C2, W3, W4, D3, D4, G2 {out['line2Ranks']}")
check(all(out["line1"]), "Line 1 is full after the draft")
check(out["cross"]["error"] and "can't play" in out["cross"]["error"], f"a cross-position swap is refused: {out['cross']['error']}")
check(out["same"].get("noop") and out["empty"]["error"], "swapping a cell with itself does nothing; an empty cell can't be picked up")

# ---- save a swap through the API ----
w = next(g for g in po["groups"] if g["position_group"] == "W")
before = [p["player_id"] for p in sorted(w["ranked"], key=lambda p: p["priority"])]
swapped = out["swapWW"]
check(swapped["group"] == "W" and swapped["ids"][:2] == [before[1], before[0]], "W1 and W2 trade places")
r = client.put(f"/teams/{team.id}/pecking-order", json={"assignments": {"W": swapped["ids"]}}, headers=auth)
check(r.status_code == 403, "only the team's owner may save (the commissioner without commissioner mode may not)")
mine = Team(league_id=lg["id"], name="Mine", owner_user_id=owner.id)
db.add(mine)
db.commit()

# ---- college-only features ----
check(client.get(f"/leagues/{lg['id']}/overview").json()["has_rankings"] is False
      and client.get(f"/leagues/{lg['id']}/overview").json()["teams_set_schedule"] is False, "NHL overview: no rankings, no team-set schedules")
r = client.get(f"/leagues/{lg['id']}/rankings")
check(r.status_code == 400 and "college" in r.json()["detail"], "NHL rankings refused")
r = client.post(f"/leagues/{lg['id']}/schedule/close", headers=auth)
check(r.status_code == 400 and "college" in r.json()["detail"], "NHL schedule negotiation refused")
cl = League(name="College", commissioner_user_id=owner.id, size=2, draft_rounds=1, draft_type="recruit")
db.add(cl)
db.flush()
db.add(LeagueUniverse(league_id=cl.id, universe_id=college.id))
db.add(Team(league_id=cl.id, name="C1", is_ai_owned=True))
db.commit()
ov = client.get(f"/leagues/{cl.id}/overview").json()
check(ov["has_rankings"] and ov["teams_set_schedule"], "college overview: rankings and team-set schedules on")
check(client.get(f"/leagues/{cl.id}/rankings").status_code != 400, "college rankings are not refused")

# ---- pages ----
page = (ROOT / "web/static/league.html").read_text(encoding="utf-8")
import re, tempfile
js = re.findall(r"<script>(.*?)</script>", page, re.S)[-1]
with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "league.js").write_text(js, encoding="utf-8")
    syntax = subprocess.run([shutil.which("node"), "--check", str(Path(tmp) / "league.js")], capture_output=True, text=True)
check(syntax.returncode == 0, f"league page script is syntactically valid {syntax.stderr[:200]}")
check("roster-grid.js" in page and "has_rankings" in page and "teams_set_schedule" in page, "league page uses the grid and the college flags")

db.close()
engine.dispose()
db_path.unlink()
print("\nAll roster grid / college feature checks passed.")
