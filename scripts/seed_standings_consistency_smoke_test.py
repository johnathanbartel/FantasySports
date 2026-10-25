"""
Standings / scoreboard / schedule consistency for NHL window leagues, on a throwaway SQLite file
(standings_consistency_smoke_test.db). One canonical final result per game (platform/game_results.py) feeds the scoreboard, the
team schedule and the series standings:

  * a skipped first window (numbering stays the scoreboard's, the skipped window keeps its number)
  * a tied final (T for both, 1 point each)
  * a final hidden from the viewer still counts in the standings, for every viewer, with the same numbers
  * finals completing out of order give the same table as in order, and repeated reads are identical
  * the table equals an independent rebuild from the canonical results (GP W L T PTS PCT GF GA HOME AWAY)
  * a competing score version: the stored mirror disagrees with the published script - the script wins everywhere,
    and finalizing never keeps the larger of two versions
  * the same two teams meeting in several series and windows count every game once
  * the standings read refreshes like the scoreboard does

    .venv-api/Scripts/python.exe scripts/seed_standings_consistency_smoke_test.py
"""

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None).start()
patch("fantasy_sports.platform.injury_sync.fetch_report", return_value=None).start()
sync_calls = []
patch("fantasy_sports.platform.nhl_daily_sync.auto_sync_league_scoreboard", side_effect=lambda db, lid: sync_calls.append(lid)).start()

db_path = Path(os.environ.get("STANDINGS_CONSISTENCY_DB", "standings_consistency_smoke_test.db"))
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.platform import game_results, series_schedule, series_standings as ss
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.models.core import (
    LeagueSeries, SeriesDailyGame, SimulatorEvent, SimulatorGame, SimulatorReveal, Team, User,
)
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.hockey_reference import seed_hockey_reference_data
from fantasy_sports.platform.simulator import service
from fantasy_sports.sports.hockey.models import Shot, ShotCredit  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(app)
db = SessionLocal()
seed_hockey_reference_data(db)
D = datetime.date


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


users = [User(email=f"u{i}@example.com", display_name=f"U{i}") for i in range(7)]
db.add_all(users)
db.commit()


def login(user):
    resp = client.post("/sessions", json={"user_id": user.id})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


H = login(users[0])
resp = client.post("/leagues", json={"name": "Consistency League", "commissioner_user_id": users[0].id, "universe_codes": ["nhl"],
                                     "size": 6}, headers=H)
check(resp.status_code == 200, resp.text)
lid = resp.json()["id"]
teams = [Team(league_id=lid, name=n, owner_user_id=users[i].id) for i, n in enumerate(["Boston", "Mesa", "Delta", "Tootoo", "Cascade", "Team3"])]
db.add_all(teams)
db.commit()
B, M, DE, TO, CA, T3 = [t.id for t in teams]
name = {t.id: t.name for t in teams}

# ---- schedule: Series 1 starts Wed Oct 14 (Game 1, Sun-Tue, was skipped); Series 2 starts Sun Oct 18 with all three windows ----
S1, S2, S3 = D(2026, 10, 14), D(2026, 10, 18), D(2026, 10, 25)
game_rows = {}          # key -> daily game id
expected = []           # (key, home, away, hs, as) final games, the independent record of what the canonical results are


def add_series(start, a, b):
    s = LeagueSeries(league_id=lid, team_a_id=a, team_b_id=b, start_date=start, end_date=start + datetime.timedelta(days=6), total_days=7)
    db.add(s)
    db.flush()
    return s


def add_game(series, key, day, end, window, home, away):
    g = SeriesDailyGame(series_id=series.id, calendar_date=day, calendar_date_end=end, window_index=window, home_team_id=home, away_team_id=away)
    db.add(g)
    db.flush()
    game_rows[key] = g.id
    return g


def publish_final(key, home, away, hs, as_, stored=None, final=True):
    """A FINAL published simulation whose last event totals are hs-as_. `stored` is what the old mirror column holds."""
    g = db.get(SeriesDailyGame, game_rows[key])
    sim = SimulatorGame(daily_game_id=g.id, contract_version="1.0", sport="hockey", periods=3, period_seconds=1200, window_seconds=3600,
                        event_count=2, script_digest=f"digest-{key}", provenance={}, status="FINAL" if final else "SCHEDULED")
    db.add(sim)
    db.flush()
    db.add(SimulatorEvent(simulator_game_id=sim.id, seq=1, kind="goal", period=1, game_second=60, team_side="home", home_score=min(hs, 1),
                          away_score=0, home_shots=1, away_shots=0, headline="early goal"))
    db.add(SimulatorEvent(simulator_game_id=sim.id, seq=2, kind="period_end", period=3, game_second=3600, team_side=None, home_score=hs,
                          away_score=as_, home_shots=hs, away_shots=as_, headline="final"))
    g.status = "FINAL" if final else "SCHEDULED"
    g.home_score, g.away_score = stored if stored else (hs, as_)
    db.flush()
    if final:
        expected.append((key, home, away, hs, as_))


s1 = {p: add_series(S1, *p) for p in [(B, M), (DE, TO), (CA, T3)]}
s2 = {p: add_series(S2, *p) for p in [(B, M), (DE, TO), (CA, T3)]}
s3 = add_series(S3, B, M)          # Boston and Mesa meet again in a later series

# Series 1: Game 2 (Wed-Thu Oct 14-15) and Game 3 (Fri-Sat Oct 16-17); Series 2: Games 1-3.
wins = {1: (S1, S1 + datetime.timedelta(1)), 2: (S1 + datetime.timedelta(2), S1 + datetime.timedelta(3))}
for (a, b), s in s1.items():
    add_game(s, f"s1g2:{a}", D(2026, 10, 14), D(2026, 10, 15), 2, a, b)
    add_game(s, f"s1g3:{a}", D(2026, 10, 16), D(2026, 10, 17), 3, b, a)
for (a, b), s in s2.items():
    add_game(s, f"s2g1:{a}", D(2026, 10, 18), D(2026, 10, 20), 1, *((b, a) if a == DE else (a, b)))     # Tootoo is home in DE/TO
    add_game(s, f"s2g2:{a}", D(2026, 10, 21), D(2026, 10, 22), 2, b, a)
    add_game(s, f"s2g3:{a}", D(2026, 10, 23), D(2026, 10, 24), 3, a, b)
add_game(s3, "s3g1", D(2026, 10, 25), D(2026, 10, 27), 1, M, B)
db.commit()

# Results. Series 1 in order; Series 2 Game 1 includes a tie (Tootoo 2, Delta 2) and a shutout (Cascade 0, Team3 6).
publish_final(f"s1g2:{B}", B, M, 4, 2)
publish_final(f"s1g3:{B}", M, B, 2, 3)
publish_final(f"s1g2:{DE}", DE, TO, 3, 1)
publish_final(f"s1g3:{DE}", TO, DE, 0, 1)
publish_final(f"s1g2:{CA}", CA, T3, 1, 2)
publish_final(f"s1g3:{CA}", T3, CA, 2, 2)                      # a tie in Series 1 too
publish_final(f"s2g1:{B}", B, M, 3, 2)
publish_final(f"s2g1:{DE}", TO, DE, 2, 2, stored=(3, 2))        # competing version: the old mirror says 3-2, the script says 2-2 (tie)
publish_final(f"s2g1:{CA}", CA, T3, 0, 6, stored=(1, 6))        # the mirror is a goal high for Cascade
db.commit()

# Series 2 Games 2 and 3 complete OUT OF ORDER: Game 3 of every matchup first, then Game 2. Everything here is hidden from
# every viewer (nobody has a reveal row, none of the teams is AI owned).
publish_final(f"s2g3:{B}", B, M, 5, 4)
publish_final(f"s2g3:{CA}", CA, T3, 1, 0)
publish_final(f"s2g3:{DE}", DE, TO, 4, 3)
db.commit()
first = client.get(f"/leagues/{lid}/series-standings").json()
for (a, b) in s2:
    publish_final(f"s2g2:{a}", b, a, 2, 1)       # b is home; Mesa/Tootoo/Team3 win 2-1 against the series' first team
db.commit()
db.commit()

OUTS = {}


def derive():
    """Independent rebuild: nothing from series_standings, only the list of canonical finals recorded above."""
    rows = {t: dict(gp=0, w=0, l=0, t=0, gf=0, ga=0, home=[0, 0, 0], away=[0, 0, 0]) for t in name}
    for _key, home, away, hs, as_ in expected:
        for tid, gf, ga, side in ((home, hs, as_, "home"), (away, as_, hs, "away")):
            r = rows[tid]
            r["gp"] += 1
            r["gf"] += gf
            r["ga"] += ga
            i = 0 if gf > ga else 1 if gf < ga else 2
            r[("w", "l", "t")[i]] += 1
            r[side][i] += 1
    for r in rows.values():
        r["pts"] = r["w"] * 3 + r["t"]
        r["pct"] = round((r["w"] + 0.5 * r["t"]) / r["gp"], 4) if r["gp"] else 0.0
    return rows


def compare(label, out, rows):
    for s in out["standings"]:
        r = rows[s["team_id"]]
        got = (s["games_played"], s["wins"], s["losses"], s["ties"], s["points"], s["win_pct"], s["goals_for"], s["goals_against"],
               [s["home_wins"], s["home_losses"], s["home_ties"]], [s["away_wins"], s["away_losses"], s["away_ties"]])
        want = (r["gp"], r["w"], r["l"], r["t"], r["pts"], r["pct"], r["gf"], r["ga"], r["home"], r["away"])
        check(got == want, f"{label}: {name[s['team_id']]} app {got} != rebuilt {want}")
    check(out["games_counted"] == len(expected), f"{label}: games_counted {out['games_counted']} != {len(expected)}")
    check(sum(s["games_played"] for s in out["standings"]) == 2 * len(expected), f"{label}: GP != 2 x games")
    check(sum(s["wins"] for s in out["standings"]) == sum(s["losses"] for s in out["standings"]), f"{label}: W != L")
    check(sum(s["goals_for"] for s in out["standings"]) == sum(s["goals_against"] for s in out["standings"]), f"{label}: GF != GA")
    check(sum(s["points"] for s in out["standings"]) == 3 * sum(s["wins"] for s in out["standings"]) + sum(s["ties"] for s in out["standings"]),
          f"{label}: points are 3 per win and 1 per tie")


rows = derive()
anon = client.get(f"/leagues/{lid}/series-standings").json()
OUTS["anon"] = anon
compare("anonymous viewer", anon, rows)

# A hidden final (nobody has watched any of them) counts for everyone, with identical numbers.
check(db.query(SimulatorGame).filter_by(status="FINAL").count() == 15 and db.query(SimulatorReveal).count() == 0,
      "15 simulated finals and no reveal rows at all")
for i, u in enumerate(users[:3]):
    out = client.get(f"/leagues/{lid}/series-standings", headers=login(u)).json()
    check(out["standings"] == anon["standings"], f"viewer {i} sees a different table than the anonymous viewer")
check(anon["games_counted"] == 15, f"15 finals expected, got {anon['games_counted']}")

# Finals completing out of order: the table taken after only Game 3 was in (before Game 2) was complete for what was final then, and
# the final table equals the in-order rebuild; a repeated read is byte-for-byte the same.
check(first["games_counted"] == 12, f"12 finals before Series 2 Game 2, got {first['games_counted']}")
check(all(s["games_played"] == 2 for s in first["standings"]) is False and sum(s["games_played"] for s in first["standings"]) == 24,
      "the earlier table is complete for the 12 games that were final then")
check(client.get(f"/leagues/{lid}/series-standings").json() == anon, "repeat read differs")
check(sync_calls and sync_calls[-1] == lid, "the standings read refreshes like the scoreboard (auto_sync_league_scoreboard)")

# Named cases.
by = {s["team_id"]: s for s in anon["standings"]}
check((by[TO]["ties"], by[DE]["ties"]) == (1, 1), "Tootoo and Delta each have the 2-2 as a tie, not a win and a loss")
# Cascade: 1-2 L, 2-2 T, 0-6 L (not the mirror's 1-6), 1-0 W, 1-2 L.
check((by[CA]["wins"], by[CA]["losses"], by[CA]["ties"], by[CA]["goals_for"], by[CA]["goals_against"]) == (1, 3, 1, 5, 12), by[CA])
check((by[T3]["goals_for"], by[T3]["goals_against"]) == (2 + 2 + 6 + 0 + 2, 1 + 2 + 0 + 1 + 1), by[T3])

# ---- the stored mirror versus the script ------------------------------------------------------------------------------
drift = {d.daily_game_id: d for d in game_results.drift(db, lid)}
check(set(drift) == {game_rows[f"s2g1:{DE}"], game_rows[f"s2g1:{CA}"]}, f"exactly the two competing versions drift, got {sorted(drift)}")
check(drift[game_rows[f"s2g1:{DE}"]].stored == (3, 2) and drift[game_rows[f"s2g1:{DE}"]].canonical == (2, 2), "Tootoo/Delta 3-2 vs 2-2")
sched = client.get(f"/leagues/{lid}/series-schedule", headers=H).json()
row = {g["id"]: g for s in sched["series"] for g in s["daily_games"]}
tie_row = row[game_rows[f"s2g1:{DE}"]]
check((tie_row["home_score"], tie_row["away_score"]) == (2, 2), f"the scoreboard shows the script's 2-2, got {tie_row['home_score']}-{tie_row['away_score']}")
check(db.get(SeriesDailyGame, game_rows[f"s2g1:{DE}"]).home_score == 3, "reading the scoreboard did not write a viewer's score into the stored game")

# Finalizing takes the script's total, never the larger of two versions.
g_new = db.get(SeriesDailyGame, game_rows["s3g1"])
sim_new = SimulatorGame(daily_game_id=g_new.id, contract_version="1.0", sport="hockey", periods=3, period_seconds=1200, window_seconds=3600,
                        event_count=1, script_digest="digest-s3", provenance={}, status="LIVE", started_at=datetime.datetime(2026, 10, 25, 12))
db.add(sim_new)
db.flush()
db.add(SimulatorEvent(simulator_game_id=sim_new.id, seq=1, kind="period_end", period=3, game_second=3600, team_side=None, home_score=1,
                      away_score=1, home_shots=1, away_shots=1, headline="final"))
g_new.status, g_new.home_score, g_new.away_score = "LIVE_SIMULATING", 3, 2     # an older, higher version left on the row
db.flush()
service._finalize(db, sim_new, db.query(SimulatorEvent).filter_by(simulator_game_id=sim_new.id).all(), datetime.datetime(2026, 10, 25, 13))
db.commit()
check((g_new.home_score, g_new.away_score) == (1, 1), f"finalize kept the older higher score: {(g_new.home_score, g_new.away_score)}")
expected.append(("s3g1", M, B, 1, 1))

# ---- the same two teams in several series and windows ---------------------------------------------------------------------
rows = derive()
out = client.get(f"/leagues/{lid}/series-standings").json()
compare("after the third Boston-Mesa series", out, rows)
bm = [e for e in expected if {e[1], e[2]} == {B, M}]
check(len(bm) == 6, f"Boston and Mesa have six finals across three series, got {len(bm)}")
riv = client.get(f"/leagues/{lid}/rivalries/{B}/{M}").json()
check(riv["daily_low_wins"] + riv["daily_high_wins"] + riv["daily_ties"] == 6, riv)
check(len(set(game_rows[e[0]] for e in bm)) == 6, "six distinct games")

# ---- scoreboard and team schedule agree on identity and numbering --------------------------------------------------------------
ts = client.get(f"/teams/{B}/series-schedule", headers=H).json()
check([s["series_number"] for s in ts["series"]] == [1, 2, 3], ts["series"])
nums = [[g["game_number"] for g in s["games"]] for s in ts["series"]]
check(nums == [[2, 3], [1, 2, 3], [1]], f"Series 1 starts at Game 2 (Game 1 skipped), got {nums}")
ts_other = client.get(f"/teams/{TO}/series-schedule", headers=H).json()
check(ts_other["team_id"] == TO and {s["opponent_name"] for s in ts_other["series"]} == {"Delta"}, "Tootoo's schedule is Tootoo's, not Boston's")
check([[g["game_number"] for g in s["games"]] for s in ts_other["series"]] == [[2, 3], [1, 2, 3]], "Tootoo has no third series")
check(series_schedule.game_number(D(2026, 10, 14)) == 2 and series_schedule.game_number(D(2026, 10, 18)) == 1 and series_schedule.game_number(D(2026, 10, 24)) == 3,
      "Sun-Tue is 1, Wed-Thu 2, Fri-Sat 3")

print("Standings consistency smoke test passed: skipped window numbering, tie, hidden final counted for every viewer, out-of-order finals, "
      "independent rebuild of all teams, competing score versions, repeated Boston-Mesa meetings, standings read refresh.")
