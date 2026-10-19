"""
Checks hockey standings by points or win percentage, the points settings, and playoff seeding, on a throwaway SQLite file
(standings_smoke_test.db): the standings maths and tiebreaks, the League Settings API (commissioner only, validated), the
series-standings output (PTS, PCT, seeds), and division winners taking the top seeds.

    .venv-api/Scripts/python.exe scripts/seed_standings_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()

db_path = Path(os.environ.get("STANDINGS_SMOKE_DB", "standings_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import playoffs, series_standings as ss
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import Conference, Division, LeagueSeries, SeriesDailyGame, Team, User
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()
seed_hockey_reference_data(db)
commissioner = User(email="commish@example.com", display_name="Commish")
other = User(email="other@example.com", display_name="Other")
db.add_all([commissioner, other])
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


H, H_OTHER = login(commissioner), login(other)

# ---- the maths: points vs win percentage, and the tiebreak after them --------------------------------------------
D = datetime.date


def game(sid, day, home, away, hs, as_):
    return ss.GameRecord(sid, day, home, away, hs, as_, True, sid * 100 + day.day)


# Team 1: 3-0-0 in 3 games (9 pts, 1.000). Team 2: 2 wins and 2 ties in 4 games (8 pts, .750 - a tie counts half). Team 3 lost
# or tied all seven; team 4 never played.
teams = [1, 2, 3, 4]
games = [
    game(1, D(2026, 1, 1), 1, 3, 3, 1), game(2, D(2026, 1, 2), 1, 3, 2, 0), game(3, D(2026, 1, 3), 1, 3, 4, 3),
    game(4, D(2026, 1, 4), 2, 3, 1, 1), game(5, D(2026, 1, 5), 2, 3, 2, 2), game(6, D(2026, 1, 6), 2, 3, 5, 0), game(7, D(2026, 1, 7), 2, 3, 3, 1),
]
series = [ss.SeriesRecord(i, 1 if i <= 3 else 2, 3, D(2026, 1, i), 1, 1) for i in range(1, 8)]
rows = {r.team_id: r for r in ss.compute_standings(teams, series, games, rules=ss.StandingsRules("points", 3, 1))}
assert (rows[1].wins, rows[1].ties, rows[1].points, rows[1].games_played) == (3, 0, 9, 3)
assert (rows[2].wins, rows[2].ties, rows[2].points) == (2, 2, 8), "2 wins + 2 ties = 8 points"
assert rows[1].win_pct == 1.0 and rows[2].win_pct == 0.75 and rows[4].win_pct == 0.0 and rows[4].points == 0
by_points = [r.team_id for r in ss.compute_standings(teams, series, games, rules=ss.StandingsRules("points", 3, 1))]
assert by_points[:2] == [1, 2], by_points
# The same results with 1 point for a win and 2 for a tie flips A and B: the points settings are the league's.
flipped = {r.team_id: r for r in ss.compute_standings(teams, series, games, rules=ss.StandingsRules("points", 1, 2))}
assert flipped[2].points == 6 and flipped[1].points == 3 and flipped[2].rank < flipped[1].rank
by_pct = [r.team_id for r in ss.compute_standings(teams, series, games, rules=ss.StandingsRules("win_pct", 3, 1))]
assert by_pct[:2] == [1, 2], by_pct
# Tie on the primary stat: goal differential decides.
tie_games = [game(1, D(2026, 1, 1), 1, 3, 5, 0), game(2, D(2026, 1, 2), 2, 4, 2, 1)]
tie_series = [ss.SeriesRecord(1, 1, 3, D(2026, 1, 1), 1, 1), ss.SeriesRecord(2, 2, 4, D(2026, 1, 2), 1, 1)]
tied = [r.team_id for r in ss.compute_standings(teams, tie_series, tie_games, rules=ss.StandingsRules("points", 3, 1))]
assert tied[:2] == [1, 2], "1 and 2 both have 3 points; the +5 goal differential beats +1"
# Without rules the original wins/losses order is unchanged.
legacy = ss.compute_standings(teams, series, games)
assert legacy[0].team_id == 1 and legacy[0].primary is None

# ---- playoff teams: the default and the seeding -------------------------------------------------------------------
assert [playoffs.default_playoff_teams(n) for n in (4, 8, 10, 12, 16, 32)] == [1, 3, 3, 4, 5, 11], "33%, rounded"
ranked = [10, 11, 12, 13, 14, 15, 16, 17]
division_of = {10: 1, 11: 1, 12: 2, 13: 1, 14: 2, 15: 1, 16: 2, 17: 2}
seeds = playoffs.seed_teams(ranked, division_of, 5)
assert [s["team_id"] for s in seeds] == [10, 12, 11, 13, 14], "division winners (10, 12) are seeds 1 and 2, then everyone else by rank"
assert [s["division_winner"] for s in seeds] == [True, True, False, False, False]
assert [s["team_id"] for s in playoffs.seed_teams(ranked, {}, 3)] == [10, 11, 12], "no divisions: by rank"
assert [s["team_id"] for s in playoffs.seed_teams(ranked, division_of, 1)] == [10], "a field smaller than the divisions"

# ---- the League Settings API ---------------------------------------------------------------------------------------
resp = client.post("/leagues", json={"name": "Saints Hockey League", "commissioner_user_id": commissioner.id,
                                      "universe_codes": ["nhl"], "size": 8}, headers=H)
assert resp.status_code == 200, resp.text
lid = resp.json()["id"]
body = client.get(f"/leagues/{lid}").json()
assert (body["standings_mode"], body["points_per_win"], body["points_per_tie"], body["playoff_teams"]) == ("points", 3, 1, None), body
team_rows = [Team(league_id=lid, name=f"Saints T{i}", owner_user_id=commissioner.id) for i in range(8)]
db.add_all(team_rows)
db.commit()
tids = [t.id for t in team_rows]
assert client.patch(f"/leagues/{lid}/settings", json={"standings_mode": "win_pct"}, headers=H_OTHER).status_code == 403
assert client.patch(f"/leagues/{lid}/settings", json={"standings_mode": "bogus"}, headers=H).status_code == 422
assert client.patch(f"/leagues/{lid}/settings", json={"points_per_win": -1}, headers=H).status_code == 422
assert client.patch(f"/leagues/{lid}/settings", json={"playoff_teams": 99}, headers=H).status_code in (400, 422), "more than the league has"
resp = client.patch(f"/leagues/{lid}/settings", json={"points_per_win": 2, "points_per_tie": 1, "playoff_teams": 4}, headers=H)
assert resp.status_code == 200 and (resp.json()["points_per_win"], resp.json()["playoff_teams"]) == (2, 4), resp.text
assert client.patch(f"/leagues/{lid}/settings", json={"playoff_teams": None}, headers=H).json()["playoff_teams"] is None, "null = the default"

# Standings settings belong to hockey leagues only.
resp = client.post("/leagues", json={"name": "Football", "commissioner_user_id": commissioner.id, "universe_codes": ["cfb"], "size": 4}, headers=H)
if resp.status_code == 200:
    assert client.patch(f"/leagues/{resp.json()['id']}/settings", json={"standings_mode": "win_pct"}, headers=H).status_code == 400

# ---- series-standings output: PTS, PCT, seeds ---------------------------------------------------------------------
day = datetime.date(2026, 2, 1)


def final(winner, loser, wg, lg):
    global day
    day += datetime.timedelta(days=1)
    s = LeagueSeries(league_id=lid, team_a_id=winner, team_b_id=loser, start_date=day, end_date=day, total_days=5, status="FINAL")
    db.add(s)
    db.flush()
    db.add(SeriesDailyGame(series_id=s.id, calendar_date=day, home_team_id=winner, away_team_id=loser, home_score=wg,
                           away_score=lg, status="FINAL"))


# t0 beats t1, t2, t3 (9 pts); t4 beats t5, t6; t5 beats t7. t3 ties are not possible in this model, so no ties here.
for loser in (1, 2, 3):
    final(tids[0], tids[loser], 3, 1)
final(tids[4], tids[5], 2, 1)
final(tids[4], tids[6], 2, 1)
final(tids[5], tids[7], 4, 0)
db.commit()
out = client.get(f"/leagues/{lid}/series-standings").json()
assert (out["standings_mode"], out["points_per_win"], out["points_per_tie"]) == ("points", 2, 1)
assert out["playoff_teams"] == 3, "8 teams, nothing set: 33% = 2.64, rounded to 3"
top = {r["team_id"]: r for r in out["standings"]}
assert top[tids[0]]["points"] == 6 and top[tids[0]]["games_played"] == 3 and top[tids[0]]["win_pct"] == 1.0
assert [r["team_id"] for r in out["standings"]][:3] == [tids[0], tids[4], tids[5]], [r["team_id"] for r in out["standings"]]
assert sorted(r["seed"] for r in out["standings"] if r["seed"]) == [1, 2, 3] and out["standings"][0]["seed"] == 1
assert "points desc" in out["tiebreak_order"]
client.patch(f"/leagues/{lid}/settings", json={"standings_mode": "win_pct", "playoff_teams": 2}, headers=H)
out = client.get(f"/leagues/{lid}/series-standings").json()
assert out["standings_mode"] == "win_pct" and out["playoff_teams"] == 2 and sum(1 for r in out["standings"] if r["seed"]) == 2

# Divisions: the division winners take the top seeds even when a better team is outranked in its own division.
conf = Conference(league_id=lid, program_size="small", name="East")
db.add(conf)
db.flush()
div_a, div_b = Division(league_id=lid, conference_id=conf.id, name="A"), Division(league_id=lid, conference_id=conf.id, name="B")
db.add_all([div_a, div_b])
db.flush()
for i, t in enumerate(team_rows):
    t.division_id = div_a.id if i in (0, 1, 2, 3) else div_b.id        # t0 tops division A; t4 tops division B
db.commit()
client.patch(f"/leagues/{lid}/settings", json={"playoff_teams": 4}, headers=H)
out = client.get(f"/leagues/{lid}/series-standings").json()
seeded = sorted((r for r in out["standings"] if r["seed"]), key=lambda r: r["seed"])
winners = [r["team_id"] for r in seeded if r["division_winner"]]
assert len(seeded) == 4 and set(winners) == {tids[0], tids[4]}, (winners, seeded)
assert {r["seed"] for r in seeded if r["division_winner"]} == {1, 2}, "division winners hold seeds 1 and 2"

# ---- the league set up wizard sends the same settings when it creates the league -----------------------------------
def create(**extra):
    return client.post("/leagues", json={"name": "Wizard", "commissioner_user_id": commissioner.id, "universe_codes": ["nhl"],
                                         "size": 12, **extra}, headers=H)


resp = create(league_format="keeper", keeper_draft_style="separate", keeper_limit=14, keeper_deadline="2099-06-01T12:00:00Z",
              pick_trade_seasons=3, standings_mode="win_pct", points_per_win=2, points_per_tie=1, playoff_teams=6)
assert resp.status_code == 200, resp.text
made = resp.json()
assert (made["league_format"], made["keeper_draft_style"], made["keeper_limit"], made["pick_trade_seasons"]) == ("keeper", "separate", 14, 3)
assert (made["standings_mode"], made["points_per_win"], made["points_per_tie"], made["playoff_teams"]) == ("win_pct", 2, 1, 6)
assert made["keeper_deadline"].startswith("2099-06-01T12:00:00"), made["keeper_deadline"]
plain = create().json()
assert (plain["league_format"], plain["standings_mode"], plain["points_per_win"], plain["points_per_tie"], plain["playoff_teams"])     == ("traditional_redraft", "points", 3, 1, None), plain
assert plain["pick_trading_enabled"] is True, "pick trading is on by default"
assert create(league_format="keeper", pick_trading_enabled=False).json()["pick_trading_enabled"] is False
assert create(playoff_teams=13).status_code == 400, "more playoff teams than the league has"
assert create(standings_mode="bogus").status_code == 422
assert create(league_format="bogus").status_code == 422
# A non-keeper league ignores a stray keeper deadline; hockey settings are rejected for other sports.
assert create(league_format="dynasty", keeper_deadline="2099-06-01T12:00:00Z").json()["keeper_deadline"] is None
nfl = client.post("/leagues", json={"name": "NFL", "commissioner_user_id": commissioner.id, "universe_codes": ["nfl"], "size": 8,
                                    "playoff_teams": 4}, headers=H)
assert nfl.status_code == 400, nfl.text
nfl_points = client.post("/leagues", json={"name": "NFL", "commissioner_user_id": commissioner.id, "universe_codes": ["nfl"], "size": 8,
                                           "standings_mode": "points"}, headers=H)
assert nfl_points.status_code == 400, "points standings are hockey only"
nfl_ok = client.post("/leagues", json={"name": "NFL", "commissioner_user_id": commissioner.id, "universe_codes": ["nfl"], "size": 8,
                                       "league_format": "dynasty", "pick_trade_seasons": 4}, headers=H)
assert nfl_ok.status_code == 200 and nfl_ok.json()["league_format"] == "dynasty" and nfl_ok.json()["pick_trade_seasons"] == 4, nfl_ok.text
assert nfl_ok.json()["standings_mode"] == "win_pct", "every sport but hockey sorts by win percentage"

# Draft order uses the league's standings mode (and shows the points kept in the archive).
print("Standings smoke test passed: points and win %, tiebreaks, settings API, series standings with seeds, division winners, wizard settings.")
