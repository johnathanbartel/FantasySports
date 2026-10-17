"""
Mathematical elimination through the real database and HTTP API on a throwaway SQLite file (elimination_smoke_test.db): the proof
writes teams.eliminated_season (which switches on the CFG-06 trade restrictions), elimination is permanent while the rules stay
put, the commissioner's tiebreaker order and playoff rules are validated and saved, changing them re-proves everything, and a
game finalising runs the proof. The proof itself is checked against brute force by scripts/verify_elimination_engine.py.

    .venv-api/Scripts/python.exe scripts/seed_elimination_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("ELIMINATION_SMOKE_DB", "elimination_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import elimination, tiebreakers
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import League, LeagueSeries, SeriesDailyGame, Team, User
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.platform.simulator import service
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

checks = 0


def check(cond, msg):
    global checks
    checks += 1
    assert cond, msg


Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()
seed_hockey_reference_data(db)
users = {n: User(email=f"{n}@example.com", display_name=n) for n in ("commish", "stranger")}
db.add_all(users.values())
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


H = login(users["commish"])
H_OTHER = login(users["stranger"])

# --- the commissioner's rules: validation at creation and in settings -------------------------------------------------------
base = {"name": "Proof", "commissioner_user_id": users["commish"].id, "universe_codes": ["nhl"], "size": 4}
check(client.post("/leagues", json={**base, "tiebreakers": ["bogus"]}, headers=H).status_code == 400, "an unknown tiebreaker is refused")
check(client.post("/leagues", json={**base, "tiebreakers": ["wins", "wins"]}, headers=H).status_code == 400, "a repeated tiebreaker is refused")
check(client.post("/leagues", json={**base, "tiebreakers": ["coin_flip", "wins"]}, headers=H).status_code == 400,
      "the random draw decides every tie, so it must come last")
check(client.post("/leagues", json={**base, "playoff_byes": 9}, headers=H).status_code == 400, "more byes than playoff spots is refused")
resp = client.post("/leagues", json={**base, "tiebreakers": ["head_to_head", "wins", "goal_differential"], "playoff_byes": 0,
                                     "division_winners_qualify": False}, headers=H)
check(resp.status_code == 200, resp.text)
out = resp.json()
lid = out["id"]
check(out["tiebreakers"] == ["head_to_head", "wins", "goal_differential"] and out["division_winners_qualify"] is False
      and out["division_winners_seeded_first"] is True and out["playoff_byes"] == 0, "the wizard's choices are saved on the league")
check(client.get(f"/leagues/{lid}").json()["tiebreakers"] == ["head_to_head", "wins", "goal_differential"], "and read back")
check(tiebreakers.order_for(db.get(League, lid)) == ("head_to_head", "wins", "goal_differential"), "the platform reads the same order")

check(client.patch(f"/leagues/{lid}/settings", json={"tiebreakers": ["wins"]}, headers=H_OTHER).status_code == 403,
      "only the commissioner changes the tiebreakers")
check(client.patch(f"/leagues/{lid}/settings", json={"tiebreakers": ["nope"]}, headers=H).status_code == 400, "settings validate the order")
resp = client.patch(f"/leagues/{lid}/settings", json={"tiebreakers": ["goal_differential", "head_to_head"], "playoff_byes": 0,
                                                       "division_winners_qualify": True}, headers=H)
check(resp.status_code == 200 and resp.json()["tiebreakers"] == ["goal_differential", "head_to_head"]
      and resp.json()["division_winners_qualify"] is True, "the commissioner can change the order and the playoff rules")
resp = client.patch(f"/leagues/{lid}/settings", json={"tiebreakers": None}, headers=H)
check(resp.status_code == 200 and resp.json()["tiebreakers"] is None, "null goes back to the default order")
check(client.patch(f"/leagues/{lid}/settings", json={"playoff_byes": 20}, headers=H).status_code == 400, "settings refuse too many byes")

# --- the proof writes the flag ---------------------------------------------------------------------------------------------
names = ["Alpha", "Bravo", "Charlie", "Delta"]
teams = [Team(league_id=lid, name=n, owner_user_id=None, is_ai_owned=True) for n in names]
db.add_all(teams)
db.commit()
A, B, C, D = (t.id for t in teams)
day = [datetime.date(2026, 10, 1)]


def game(home, away, home_score=None, away_score=None):
    day[0] += datetime.timedelta(days=1)
    s = LeagueSeries(league_id=lid, team_a_id=home, team_b_id=away, start_date=day[0], end_date=day[0], total_days=5)
    db.add(s)
    db.flush()
    final = home_score is not None
    db.add(SeriesDailyGame(series_id=s.id, calendar_date=day[0], home_team_id=home, away_team_id=away,
                           home_score=home_score or 0, away_score=away_score or 0, status="FINAL" if final else "SCHEDULED"))


def flags():
    db.expire_all()
    league = db.get(League, lid)
    return {t.id: elimination.is_eliminated(league, t) for t in db.query(Team).filter_by(league_id=lid)}


for _ in range(3):
    game(A, B, 3, 1)       # A beats B three times: 9 points; the default field is one team (33% of four, rounded)
    game(C, D, 2, 1)       # C beats D three times
game(A, C)                 # games still to play
game(B, D)
db.commit()
league = db.get(League, lid)
newly = elimination.recalculate(db, league)
db.commit()
check(set(newly) == {B, D}, f"B and D (no points, three to gain at most) cannot reach A or C: proven out ({newly})")
check(flags() == {A: False, B: True, C: False, D: True}, "A and C can both still finish first: neither is out")

# A beats C, and now C (9 points, 3 wins) cannot catch A (12): A is through, B, C and D are out.
day[0] = datetime.date(2026, 10, 20)
for g in db.query(SeriesDailyGame).filter(SeriesDailyGame.status != "FINAL"):
    if g.home_team_id == A:
        g.status, g.home_score, g.away_score = "FINAL", 4, 0
db.commit()
newly = elimination.recalculate_league(db, lid)
db.commit()
check(newly == [C], f"C (9 points, nothing to gain) is now out too; B and D were already marked ({newly})")
f = flags()
check(f[B] and f[C] and f[D] and not f[A], "only the leader is alive")

# it is permanent: re-running changes nothing, and re-running with the same rules keeps the flag
check(elimination.recalculate_league(db, lid) == [], "nothing new the second time")
db.commit()
check(flags()[C], "the flag stays")

# the standings API shows who is out and what the rules are
body = client.get(f"/leagues/{lid}/series-standings").json()
rows = {r["team_id"]: r for r in body["standings"]}
check(rows[C]["eliminated"] and not rows[A]["eliminated"], "the standings carry the elimination flag")
check(body["tiebreakers"] == list(tiebreakers.DEFAULT_ORDER) and body["playoff_byes"] == 0, "and the tiebreaker order in use")
check(rows[A]["seed"] == 1 and rows[A]["bye"] is False, "A holds the one seed, no bye with byes at zero")
client.patch(f"/leagues/{lid}/settings", json={"playoff_teams": 2, "playoff_byes": 1}, headers=H)
body = client.get(f"/leagues/{lid}/series-standings").json()
check(body["playoff_byes"] == 1 and {r["team_id"]: r["bye"] for r in body["standings"]}[A] is True, "a bye goes to the top seed")

# changing the playoff field re-proves everything: with two spots C is second, so the old flag is cleared when the rules change
f = flags()
check(not f[C] and not f[A], "with a two-team field C can still make it: its flag was cleared by the settings change")
check(f[B] and f[D], "B and D are out under either field")

# --- the hook: finalising a game runs the proof ----------------------------------------------------------------------------
fake_game = SimpleNamespace(status="LIVE", home_score=0, away_score=0, current_period=0, seconds_remaining=1200, series_id=1)
series = SimpleNamespace(id=1, league_id=77, status="IN_PROGRESS")
fake_db = MagicMock()
fake_db.get.return_value = series
fake_db.query.return_value.filter_by.return_value.all.return_value = [fake_game]
sim = SimpleNamespace(daily_game=fake_game, status="LIVE", finalized_at=None)
with patch.object(service, "shape_of", return_value=SimpleNamespace(periods=3)), \
        patch.object(elimination, "recalculate_league") as proof:
    service._finalize(fake_db, sim, [SimpleNamespace(home_score=3, away_score=1)], datetime.datetime(2026, 10, 30))
check(proof.call_count == 1 and proof.call_args.args[1] == 77, "finalising a game runs the proof for its league")

# a failing proof never stops a game from finalising
with patch.object(elimination, "recalculate", side_effect=RuntimeError("boom")):
    check(elimination.recalculate_league(db, lid) == [], "an error in the proof is swallowed, the game still finalises")

# --- offseason and other sports --------------------------------------------------------------------------------------------
league = db.get(League, lid)
league.offseason = True
db.commit()
check(elimination.compute(db, league) is None and not any(flags().values()), "offseason: nobody is eliminated")
league.offseason = False
league.series_length_days = None
check(elimination.snapshot_for(db, league) is None, "a sport without an adapter has nothing to prove from yet")
db.rollback()

print(f"Elimination smoke test passed ({checks} checks).")
db.close()
engine.dispose()
db_path.unlink(missing_ok=True)
