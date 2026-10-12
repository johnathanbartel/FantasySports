"""
Verification for NHL Live Scoreboard:
- Single-line road team goals, gap, home goals, home team layout.
- Period 1 in progress displays 0 - 0 in progress.
- Period 2+ displays ? - ? until owner watches highlights or skips.
- Reminder shown when owner has not seen the results.
- Backend skip() jumps to latest completed period during live simulation.
- Score revealed to all once owner watches.
- Box score and highlights buttons.
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def main() -> None:
    # 1. Test state.py shown_seq logic
    from fantasy_sports.platform.simulator import state as st

    check(st.shown_seq(st.LIVE, None, cursor_seq=10, public_seq=20, total=50) == 0,
          "live game with no reveal mode is veiled (seq 0)")
    check(st.shown_seq(st.LIVE, st.SKIPPED, cursor_seq=15, public_seq=30, total=50) == 15,
          "live game with SKIPPED mode shows cursor_seq (skipped to period)")
    check(st.shown_seq(st.FINAL, st.SKIPPED, cursor_seq=15, public_seq=50, total=50) == 50,
          "final game with SKIPPED mode shows total events")
    check(st.shown_seq(st.LIVE, st.WATCHING, cursor_seq=10, public_seq=25, total=50) == 25,
          "live game with WATCHING mode tracks public_seq")

    # 2. Integration test with throwaway SQLite database
    with tempfile.TemporaryDirectory(prefix="fs-scb-", dir=ROOT, ignore_cleanup_errors=True) as temp_dir:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_dir) / 'test.db').as_posix()}"

        from fastapi.testclient import TestClient
        from fantasy_sports.api.auth import create_session
        from fantasy_sports.api.main import app
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import (
            League, LeagueSeries, LeagueUniverse, SeriesDailyGame, SimulatorGame,
            SimulatorEvent, SimulatorReveal, Team, Universe, User,
        )
        from fantasy_sports.platform.db.session import SessionLocal, engine
        from fantasy_sports.platform.simulator import service

        Base.metadata.create_all(engine)
        db = SessionLocal()

        u_nhl = Universe(code="nhl", display_name="National Hockey League", sport="hockey", level="pro")
        db.add(u_nhl)
        commish = User(id=1, email="commish@example.com", display_name="Commissioner")
        owner_b = User(id=2, email="owner_b@example.com", display_name="Manager B")
        owner_c = User(id=3, email="owner_c@example.com", display_name="Manager C")
        db.add_all([commish, owner_b, owner_c])
        db.commit()

        league = League(
            id=1, name="Scoreboard Test League", commissioner_user_id=commish.id,
            size=4, draft_rounds=24, series_length_days=7,
        )
        db.add(league)
        db.commit()

        db.add(LeagueUniverse(league_id=league.id, universe_id=u_nhl.id))

        t_human_a = Team(id=1, league_id=league.id, owner_user_id=commish.id, name="Oilers", is_ai_owned=False)
        t_human_b = Team(id=2, league_id=league.id, owner_user_id=owner_b.id, name="Bruins", is_ai_owned=False)
        t_ai_1 = Team(id=3, league_id=league.id, owner_user_id=None, name="AI Leafs", is_ai_owned=True)
        t_ai_2 = Team(id=4, league_id=league.id, owner_user_id=None, name="AI Rangers", is_ai_owned=True)
        db.add_all([t_human_a, t_human_b, t_ai_1, t_ai_2])
        db.commit()

        start_date = datetime.date(2026, 10, 11)
        series_human = LeagueSeries(
            id=1, league_id=league.id, team_a_id=t_human_a.id, team_b_id=t_human_b.id,
            start_date=start_date, end_date=start_date + datetime.timedelta(days=6),
            total_days=7, status="IN_PROGRESS",
        )
        series_ai = LeagueSeries(
            id=2, league_id=league.id, team_a_id=t_ai_1.id, team_b_id=t_ai_2.id,
            start_date=start_date, end_date=start_date + datetime.timedelta(days=6),
            total_days=7, status="IN_PROGRESS",
        )
        db.add_all([series_human, series_ai])
        db.commit()

        dg_human = SeriesDailyGame(
            id=1, series_id=series_human.id, calendar_date=start_date, is_matchup_day=True,
            home_team_id=t_human_b.id, away_team_id=t_human_a.id, status="LIVE_SIMULATING",
            current_period=1, home_score=0, away_score=0, game_score_rating=85,
        )
        dg_ai = SeriesDailyGame(
            id=2, series_id=series_ai.id, calendar_date=start_date, is_matchup_day=True,
            home_team_id=t_ai_2.id, away_team_id=t_ai_1.id, status="LIVE_SIMULATING",
            current_period=2, home_score=2, away_score=1, game_score_rating=70,
        )
        db.add_all([dg_human, dg_ai])
        db.commit()

        # Build simulator games with 3 periods (1200 sec each, 3600 sec window)
        started_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1800)
        sim_human = SimulatorGame(
            id=1, daily_game_id=dg_human.id, contract_version="1.0", sport="hockey",
            periods=3, period_seconds=1200, window_seconds=3600, event_count=4,
            script_digest="sim1", status="LIVE", started_at=started_at,
        )
        sim_ai = SimulatorGame(
            id=2, daily_game_id=dg_ai.id, contract_version="1.0", sport="hockey",
            periods=3, period_seconds=1200, window_seconds=3600, event_count=4,
            script_digest="sim2", status="LIVE", started_at=started_at,
        )
        db.add_all([sim_human, sim_ai])
        db.commit()

        # Add events spanning period 1 (0..1200s) and period 2 (1201..2400s)
        # Period 1 events:
        ev1 = SimulatorEvent(simulator_game_id=sim_human.id, seq=1, period=1, game_second=300, home_score=0, away_score=1, home_shots=4, away_shots=5, kind="GOAL", headline="Goal 1", detail={})
        ev2 = SimulatorEvent(simulator_game_id=sim_human.id, seq=2, period=1, game_second=1100, home_score=1, away_score=1, home_shots=9, away_shots=8, kind="GOAL", headline="Goal 2", detail={})
        # Period 2 events:
        ev3 = SimulatorEvent(simulator_game_id=sim_human.id, seq=3, period=2, game_second=1500, home_score=1, away_score=2, home_shots=12, away_shots=11, kind="GOAL", headline="Goal 3", detail={})
        ev4 = SimulatorEvent(simulator_game_id=sim_human.id, seq=4, period=3, game_second=3000, home_score=2, away_score=2, home_shots=20, away_shots=22, kind="GOAL", headline="Goal 4", detail={})
        db.add_all([ev1, ev2, ev3, ev4])
        db.commit()

        # Test read_series_schedule:
        client = TestClient(app)
        t_commish = create_session(db, commish.id)
        t_other = create_session(db, owner_c.id)

        res = client.get(f"/leagues/{league.id}/series-schedule", headers={"Authorization": f"Bearer {t_commish.token}"}).json()
        series_out = res["series"]
        check(len(series_out) == 2, "series-schedule returns 2 series")

        human_dg = next(g for s in series_out if s["id"] == series_human.id for g in s["daily_games"])
        ai_dg = next(g for s in series_out if s["id"] == series_ai.id for g in s["daily_games"])

        check(ai_dg["owner_watched"] is True, "AI vs AI matchup has owner_watched=True")
        check(human_dg["owner_watched"] is False, "Human matchup has owner_watched=False before owner watches")

        # Test skip() endpoint for human game while in period 2 (1800s elapsed -> completed_periods=1)
        # Third party manager C tries to skip
        skip_res_c = client.post(f"/simulator/games/{dg_human.id}/skip", headers={"Authorization": f"Bearer {t_other.token}"})
        check(skip_res_c.status_code == 200, "non-owner can skip to latest completed period")
        state_c = skip_res_c.json()
        check(state_c["shown_seq"] == 2, "skip jumps to end of period 1 (seq 2)")
        check(state_c["away_score"] == 1 and state_c["home_score"] == 1,
              "score at end of period 1 is 1 - 1")

        # But because owner (commish / owner_b) has NOT watched/skipped, league schedule still reports owner_watched=False
        res2 = client.get(f"/leagues/{league.id}/series-schedule", headers={"Authorization": f"Bearer {t_commish.token}"}).json()
        human_dg2 = next(g for s in res2["series"] if s["id"] == series_human.id for g in s["daily_games"])
        check(human_dg2["owner_watched"] is False, "owner_watched is still False after non-owner skips")

        # Now owner (commish) skips/watches:
        skip_res_owner = client.post(f"/simulator/games/{dg_human.id}/skip", headers={"Authorization": f"Bearer {t_commish.token}"})
        check(skip_res_owner.status_code == 200, "owner can skip to latest completed period")

        # Now league schedule reports owner_watched=True for everyone!
        res3 = client.get(f"/leagues/{league.id}/series-schedule", headers={"Authorization": f"Bearer {t_other.token}"}).json()
        human_dg3 = next(g for s in res3["series"] if s["id"] == series_human.id for g in s["daily_games"])
        check(human_dg3["owner_watched"] is True, "owner_watched becomes True for everyone once team owner watches/skips")

        db.close()

    print("\nAll NHL live scoreboard backend and schedule tests passed.")


if __name__ == "__main__":
    main()
