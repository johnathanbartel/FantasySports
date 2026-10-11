"""
Offline verification for the NHL series schedule engine and APIs (FS-021).

Builds a 12-team NHL league in a throwaway SQLite database, generates the schedule through the real HTTP
API (FastAPI TestClient), and checks the scheduling invariants, authorization, validation, replace-only
regeneration, and that football (non-NHL) leagues are untouched. No external services are called.

Run from the repository root with:

    .venv-api/Scripts/python.exe scripts/verify_hockey_series_schedule.py
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fs021-series-", dir=ROOT) as temp_dir:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir) / 'series-schedule').as_posix()}"

        from fastapi.testclient import TestClient

        from fantasy_sports.api.auth import create_session
        from fantasy_sports.api.main import app
        from fantasy_sports.platform import series_schedule
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            League, LeagueUniverse, SeriesDailyGame, Team, Universe, User,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine

        Base.metadata.create_all(bind=engine)
        client = TestClient(app)
        start = datetime.date(2026, 10, 7)

        with SessionLocal() as db:
            commissioner = User(email="c@example.com", display_name="Commissioner")
            other = User(email="o@example.com", display_name="Other")
            db.add_all([commissioner, other])
            nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
            cfb = Universe(sport="football", level="college", code="cfb", display_name="CFB", supports_individual_defense=True)
            db.add_all([nhl, cfb])
            db.flush()

            def make_league(name: str, universe: Universe, teams: int, length: int | None) -> League:
                league = League(name=name, commissioner_user_id=commissioner.id, size=teams, series_length_days=length)
                db.add(league)
                db.flush()
                db.add(LeagueUniverse(league_id=league.id, universe_id=universe.id))
                for i in range(teams):
                    db.add(Team(league_id=league.id, name=f"{name} T{i + 1}", is_ai_owned=True))
                db.flush()
                return league

            nhl12 = make_league("NHL12", nhl, 12, 7)
            nhl8_ten = make_league("NHL8", nhl, 8, 10)
            nhl6_five = make_league("NHL6", nhl, 6, 5)
            odd = make_league("NHL5", nhl, 5, 7)
            football = make_league("CFB", cfb, 12, None)
            db.commit()
            ids = {k: v.id for k, v in dict(nhl12=nhl12, nhl8=nhl8_ten, nhl6=nhl6_five, odd=odd, football=football).items()}
            team_ids12 = [t.id for t in db.query(Team).filter_by(league_id=nhl12.id)]
            token = create_session(db, commissioner.id).token
            other_token = create_session(db, other.id).token

        auth = {"Authorization": f"Bearer {token}"}
        body = {"start_date": start.isoformat()}

        # Authorization.
        url12 = f"/leagues/{ids['nhl12']}/series-schedule"
        check(client.post(url12, json=body).status_code == 401, "creation requires a signed-in user")
        check(client.post(url12, json=body, headers={"Authorization": f"Bearer {other_token}"}).status_code == 403,
              "creation is commissioner-only")

        # Validation of unsupported shapes.
        r = client.post(f"/leagues/{ids['odd']}/series-schedule", json=body, headers=auth)
        check(r.status_code == 400 and "even" in r.json()["detail"], "odd-sized league is rejected")
        r = client.post(f"/leagues/{ids['football']}/series-schedule", json=body, headers=auth)
        check(r.status_code == 400 and "NHL" in r.json()["detail"], "non-NHL league is rejected")
        check(client.get(f"/leagues/{ids['football']}/series-schedule").status_code == 400, "non-NHL read is rejected")
        r = client.post(url12, json={**body, "cycles": 9}, headers=auth)
        check(r.status_code == 400, "out-of-range cycles are rejected")
        check(client.get(url12).json()["series"] == [], "failed attempts left no schedule behind")

        # 12-team, 7-day league.
        r = client.post(url12, json=body, headers=auth)
        check(r.status_code == 200, "12-team schedule created")
        first = r.json()
        series = first["series"]
        check(first["series_length_days"] == 7, "league series length (7) is used")
        check(len(series) == 11 * 6, "11 rounds x 6 series per round")

        by_round: dict[str, list] = defaultdict(list)
        for s in series:
            by_round[s["start_date"]].append(s)
        check(len(by_round) == 11, "11 distinct series start dates")
        for k, (d, round_series) in enumerate(sorted(by_round.items())):
            expected = start + datetime.timedelta(days=7 * k)
            assert d == expected.isoformat(), f"round {k + 1} must start {expected} (back to back)"
            teams = Counter(t for s in round_series for t in (s["team_a_id"], s["team_b_id"]))
            assert sorted(teams) == sorted(team_ids12) and set(teams.values()) == {1}, f"round {k + 1} must use each team once"
            for s in round_series:
                assert s["total_days"] == 7 and len(s["daily_games"]) == 7
                assert s["end_date"] == (datetime.date.fromisoformat(s["start_date"]) + datetime.timedelta(days=6)).isoformat()
        check(True, "every team has exactly one opponent in every round")

        pairs = Counter(frozenset((s["team_a_id"], s["team_b_id"])) for s in series)
        check(len(pairs) == 66 and set(pairs.values()) == {1}, "each of the 66 team pairs meets exactly once")

        team_date: Counter = Counter()
        for s in series:
            opponents = set()
            for g in s["daily_games"]:
                team_date[(g["home_team_id"], g["calendar_date"])] += 1
                team_date[(g["away_team_id"], g["calendar_date"])] += 1
                opponents.add(frozenset((g["home_team_id"], g["away_team_id"])))
                check_ok = g["is_matchup_day"] and g["status"] == "SCHEDULED"
                assert check_ok, "daily games start as scheduled matchup days"
            assert opponents == {frozenset((s["team_a_id"], s["team_b_id"]))}, "one opponent across the whole series"
        check(set(team_date.values()) == {1} and len(team_date) == 12 * 77, "no team plays twice on a date; 77 days each")
        check(True, "one opponent across all days of every series")

        first_series = series[0]
        homes = [g["home_team_id"] for g in first_series["daily_games"]]
        check(homes[0] == first_series["team_a_id"] and homes[1] == first_series["team_b_id"]
              and homes[2] == first_series["team_a_id"], "home/away alternates by day starting with team_a")

        with SessionLocal() as db:
            check(db.query(SeriesDailyGame).count() == 11 * 6 * 7, "462 daily-game rows persisted")

        # Duplicate generation: replace-only contract.
        r = client.post(url12, json=body, headers=auth)
        check(r.status_code == 400 and "replace" in r.json()["detail"], "repeat generation without replace is rejected")
        check(client.get(url12).json() == first, "rejected repeat leaves the schedule byte-identical")
        r = client.post(url12, json={**body, "replace": True}, headers=auth)
        again = r.json()

        def shape(payload):
            return [(s["team_a_id"], s["team_b_id"], s["start_date"], s["end_date"],
                     [(g["calendar_date"], g["home_team_id"], g["away_team_id"]) for g in s["daily_games"]])
                    for s in payload["series"]]
        check(r.status_code == 200 and shape(again) == shape(first), "replace=true regenerates the identical schedule")
        with SessionLocal() as db:
            check(db.query(SeriesDailyGame).count() == 462, "replace leaves no duplicate rows")
        r = client.post(url12, json={"start_date": (start + datetime.timedelta(days=14)).isoformat(), "replace": True}, headers=auth)
        check(r.status_code == 200 and r.json()["series"][0]["start_date"] == (start + datetime.timedelta(days=14)).isoformat(),
              "replace with a new start date moves the schedule")

        # Replace is refused once play has started.
        with SessionLocal() as db:
            game = db.query(SeriesDailyGame).first()
            game.status = "FINAL"
            db.commit()
        r = client.post(url12, json={**body, "replace": True}, headers=auth)
        check(r.status_code == 400 and "started" in r.json()["detail"], "replace is refused after a game has started")
        with SessionLocal() as db:
            db.query(SeriesDailyGame).first().status = "SCHEDULED"
            db.commit()

        # Team schedule read.
        tid = team_ids12[0]
        r = client.get(f"/teams/{tid}/series-schedule")
        ts = r.json()
        check(r.status_code == 200 and len(ts["series"]) == 11, "team schedule lists 11 series")
        check(len({s["opponent_id"] for s in ts["series"]}) == 11 and tid not in {s["opponent_id"] for s in ts["series"]},
              "team faces 11 distinct opponents")
        g0 = ts["series"][0]["games"][0]
        check(set(g0) == {"daily_game_id", "calendar_date", "is_matchup_day", "home_away", "status"}
              and g0["home_away"] in ("home", "away"), "team games expose date, home/away, off-day input, and status")
        check(ts["series"][0]["opponent_name"].startswith("NHL12 T"), "team schedule names the opponent")
        check(client.get("/teams/99999/series-schedule").status_code == 404, "unknown team is 404")

        # 8-team 10-day and 6-team 5-day leagues, plus a double round-robin.
        r = client.post(f"/leagues/{ids['nhl8']}/series-schedule", json=body, headers=auth).json()
        check(len(r["series"]) == 7 * 4 and all(s["total_days"] == 10 for s in r["series"]), "8-team 10-day league: 7 rounds x 4 series")
        r = client.post(f"/leagues/{ids['nhl6']}/series-schedule", json={**body, "cycles": 2}, headers=auth).json()
        check(len(r["series"]) == 2 * 5 * 3 and all(s["total_days"] == 5 for s in r["series"]), "6-team 5-day league, 2 cycles")
        c1 = [s for s in r["series"] if datetime.date.fromisoformat(s["start_date"]) < start + datetime.timedelta(days=25)]
        c2 = [s for s in r["series"] if datetime.date.fromisoformat(s["start_date"]) >= start + datetime.timedelta(days=25)]
        check(len(c1) == len(c2) == 15 and {(s["team_a_id"], s["team_b_id"]) for s in c1} == {(s["team_b_id"], s["team_a_id"]) for s in c2},
              "second cycle swaps home orientation")

        # Pure planner determinism and the football weekly tables staying untouched.
        a = series_schedule.plan_series(team_ids12, 7, start)
        check(a == series_schedule.plan_series(list(reversed(team_ids12)), 7, start), "planner is deterministic regardless of input order")
        try:
            series_schedule.plan_series(team_ids12, 6, start)
        except ValueError:
            pass
        else:
            raise AssertionError("length 6 should be rejected")
        check(True, "planner rejects unsupported series length")
        with SessionLocal() as db:
            from fantasy_sports.platform.db.models.core import WeeklyMatchup
            check(db.query(WeeklyMatchup).count() == 0, "weekly_matchups untouched")
        engine.dispose()  # release the SQLite file so the temp directory can be removed

    print("\nAll NHL series schedule checks passed.")


if __name__ == "__main__":
    main()
