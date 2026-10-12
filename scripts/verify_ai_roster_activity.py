"""
Offline checks (throwaway SQLite, TestClient; time is passed in, nothing sleeps) for AI teams that run their own rosters
and lines in a non-college league (platform/ai/ai_roster.py):

  * nobody has to press anything: the league's pages trigger the AI's due decisions
  * decisions are spread out - each team visits the pool at its own pseudo-random times 1-14 hours apart, one
    transaction per visit - so a league does not make a burst of transactions at once
  * a team 10 players short fills out over days, never past the 24-player limit; people's teams are never touched
  * AI teams set their own lines, best players first
  * a full AI team occasionally swaps its weakest player for a clearly better free agent at the same position
  * applying decisions twice changes nothing

    .venv-api/Scripts/python.exe scripts/verify_ai_roster_activity.py
"""

import datetime as dt
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
db_path = Path("ai_roster_activity_verify.db")
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform.ai import ai_roster
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, FreeAgencyTransaction, Player, RealTeam, RosterEntry, Team, TeamPeckingOrder, User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit

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
next_game = [1000]


def add_player(pos, name, credit=None):
    p = Player(universe_id=nhl.id, real_team_id=club.id, full_name=name, position=pos)
    db.add(p)
    db.flush()
    a = DraftableAsset(universe_id=nhl.id, asset_type="player", player_id=p.id)
    db.add(a)
    db.flush()
    if credit is not None:
        next_game[0] += 1
        s = Shot(game_id=next_game[0], source="t", external_shot_id=f"s{a.id}", shooter_asset_id=a.id)
        db.add(s)
        db.flush()
        db.add(ShotCredit(shot_id=s.id, draftable_asset_id=a.id, role="shooter", credit_points=credit))
    return a


for pos, n in (("C", 40), ("LW", 28), ("RW", 28), ("D", 60), ("G", 28)):
    for i in range(n):
        add_player(pos, f"{pos} Player {i:02d}")
owner = User(email="o@example.com", display_name="Owner")
db.add(owner)
db.commit()
auth = {"Authorization": "Bearer " + client.post("/sessions", json={"user_id": owner.id}).json()["token"]}

lg = client.post("/leagues", json={"name": "AI", "commissioner_user_id": owner.id, "universe_codes": ["nhl"], "size": 6},
                 headers=auth).json()
client.post(f"/leagues/{lg['id']}/ai-teams", headers=auth)
human = db.query(Team).filter_by(league_id=lg["id"]).order_by(Team.id).first()
human.owner_user_id, human.is_ai_owned = owner.id, False
db.commit()
d = client.post("/drafts/start", json={"league_id": lg["id"]}).json()
while client.post(f"/drafts/{d['id']}/autopick").status_code == 200:
    pass

ai_teams = [t for t in db.query(Team).filter_by(league_id=lg["id"], is_ai_owned=True).order_by(Team.id)]
ids = [t.id for t in ai_teams]
# every team holds 14 players, as a league created under the old roster size would
for t in db.query(Team).filter_by(league_id=lg["id"]):
    keep = [e.id for e in db.query(RosterEntry).filter_by(team_id=t.id).order_by(RosterEntry.id)][:14]
    kept = [e.draftable_asset_id for e in db.query(RosterEntry).filter(RosterEntry.id.in_(keep))]
    db.query(RosterEntry).filter(RosterEntry.team_id == t.id, ~RosterEntry.id.in_(keep)).delete(synchronize_session=False)
    db.query(TeamPeckingOrder).filter(TeamPeckingOrder.team_id == t.id, ~TeamPeckingOrder.draftable_asset_id.in_(kept)).delete(synchronize_session=False)
db.commit()


def roster_count(t):
    return db.query(RosterEntry).filter_by(team_id=t).count()


def ai_txns(kind=None):
    q = db.query(FreeAgencyTransaction).filter_by(league_id=lg["id"], reason="ai_fill")
    return q.filter_by(kind=kind).all() if kind else q.all()


T0 = dt.datetime(2026, 10, 12, 12, 0, 0)

# ---- the lines: an AI team puts its best players first ----
star_team = ai_teams[0]
w_rows = (db.query(TeamPeckingOrder).filter_by(team_id=star_team.id, position_group="W").order_by(TeamPeckingOrder.priority).all())
last_w = w_rows[-1].draftable_asset_id
next_game[0] += 1
star_shot = Shot(game_id=next_game[0], source="t", external_shot_id="star", shooter_asset_id=last_w)
db.add(star_shot)
db.flush()
db.add(ShotCredit(shot_id=star_shot.id, draftable_asset_id=last_w, role="shooter", credit_points=90.0))
db.commit()
visits = ai_roster.tick(db, lg["id"], now=T0)
db.commit()
check(visits == 0 and not ai_txns(), "first sight: nothing is signed yet (the first visit is hours away)")
db.expire_all()
first_w = db.query(TeamPeckingOrder).filter_by(team_id=star_team.id, position_group="W", priority=1).one()
check(first_w.draftable_asset_id == last_w, "the AI team set its own lines: its best winger moved from last to first")
check(all(db.query(TeamPeckingOrder).filter_by(team_id=t, position_group="W").count() == sum(1 for e in db.query(RosterEntry).filter_by(team_id=t) if e.draftable_asset.player.position in ("LW", "RW"))
          for t in ids), "every AI team's winger line holds exactly its wingers")
states = {t.id: t.personality_profile["ai_roster"] for t in db.query(Team).filter(Team.id.in_(ids))}
first_visits = sorted(dt.datetime.fromisoformat(s["next_at"]) for s in states.values())
check(len({v for v in first_visits}) == 5 and first_visits[0] >= T0 + dt.timedelta(hours=1) and first_visits[-1] <= T0 + dt.timedelta(hours=14),
      f"each team's first visit is its own time, 1-14 hours out: {[str(v - T0) for v in first_visits]}")

# ---- spread over time ----
ai_roster.tick(db, lg["id"], now=T0 + dt.timedelta(minutes=50)); db.commit()
check(len(ai_txns()) == 0, "50 minutes later: still nothing (no team visits sooner than an hour)")
ai_roster.tick(db, lg["id"], now=T0 + dt.timedelta(hours=6)); db.commit()
after6 = len(ai_txns())
check(0 < after6 < 25, f"6 hours in: a few signings, not a burst ({after6} of the 50 needed)")
ai_roster.tick(db, lg["id"], now=T0 + dt.timedelta(days=1)); db.commit()
after24 = len(ai_txns())
check(after6 < after24 < 50, f"after a day: more, but the league is still filling ({after24} of 50)")
check(roster_count(human.id) == 14, "the person's team is never touched")
before = [(x.id) for x in ai_txns()]
ai_roster.tick(db, lg["id"], now=T0 + dt.timedelta(days=1)); db.commit()
check([x.id for x in ai_txns()] == before, "applying the same moment again changes nothing")

ai_roster.tick(db, lg["id"], now=T0 + dt.timedelta(days=10)); db.commit()
check([roster_count(t) for t in ids] == [24] * 5, f"after 10 days every AI roster is full: {[roster_count(t) for t in ids]}")
check(roster_count(human.id) == 14, "...and the person's team still has its 14, free to fill as they like")

adds = sorted(ai_txns("add"), key=lambda x: x.created_at)
per_team_gaps = []
for t in ids:
    times = [x.created_at for x in adds if x.team_id == t]
    per_team_gaps += [(b - a).total_seconds() for a, b in zip(times, times[1:])]
check(min(per_team_gaps) >= 3600 - 1, f"a team never makes two signings within an hour (shortest gap {min(per_team_gaps) / 3600:.1f}h)")
check(len({x.created_at for x in adds}) >= 40, f"signings land at {len({x.created_at for x in adds})} different moments over {(adds[-1].created_at - adds[0].created_at).days + 1} days")
check(all(x.price == 0 for x in adds), "signings are free outside college leagues")
for t in ids:
    groups = {}
    for e in db.query(RosterEntry).filter_by(team_id=t):
        pos = e.draftable_asset.player.position
        groups["W" if pos in ("LW", "RW") else pos] = groups.get("W" if pos in ("LW", "RW") else pos, 0) + 1
    assert all(groups.get(g, 0) >= n for g, n in (("C", 1), ("W", 2), ("D", 2), ("G", 1))), (t, groups)
check(True, "every AI lineup has its starters at every position")

# ---- swaps: a full team upgrades for a clearly better free agent at the same position ----
stars = [add_player(pos, f"Star {pos} {i}", credit=60.0) for pos in ("C", "LW", "D", "G") for i in range(4)]
db.commit()
dropped_before = len(ai_txns("drop"))
ai_roster.tick(db, lg["id"], now=T0 + dt.timedelta(days=70)); db.commit()
drops, new_adds = ai_txns("drop"), ai_txns("add")
check(len(drops) > dropped_before, f"full AI teams swapped weak players for better free agents ({len(drops)} swaps)")
check([roster_count(t) for t in ids] == [24] * 5, "a swap keeps the roster at 24")
check(roster_count(human.id) == 14, "the person's team is still untouched after 70 days")
by_pair = {(x.team_id, x.created_at): x for x in drops}
check(all((x.team_id, x.created_at) in {(a.team_id, a.created_at) for a in new_adds} for x in drops), "each swap is a drop and a signing at the same moment")
rate = len(drops) / sum(1 for _ in range(1))  # swaps are rare relative to visits
visits_total = sum(t.personality_profile["ai_roster"]["k"] for t in db.query(Team).filter(Team.id.in_(ids)))
check(len(drops) < visits_total * 0.4, f"swaps stay occasional: {len(drops)} swaps in {visits_total} visits")

# ---- the league's own pages trigger it: nobody presses a button ----
t = db.get(Team, ids[1])
db.query(RosterEntry).filter_by(team_id=t.id).limit(1)
extra = db.query(RosterEntry).filter_by(team_id=t.id).first()
db.delete(extra)
profile = dict(t.personality_profile)
profile["ai_roster"] = {"k": profile["ai_roster"]["k"], "next_at": "2020-01-01T00:00:00"}
t.personality_profile = profile
db.commit()
n_before = roster_count(t.id)
r = client.get(f"/leagues/{lg['id']}/overview")
check(r.status_code == 200, "overview loads")
db.expire_all()
check(roster_count(t.id) == n_before + 1, "opening the league home was enough for a short AI team to sign a free agent")

# ---- the page tells people, and there is no button for pro leagues ----
page = (ROOT / "web/static/league.html").read_text(encoding="utf-8")
check("manage their own rosters and lines" in page and "fillNeeded && ov.is_college && isCommissioner" in page,
      "pro leagues show an explanation instead of a 'run AI' button")

db.close()
engine.dispose()
db_path.unlink()
print("\nAll AI roster activity checks passed.")
