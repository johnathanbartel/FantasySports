r"""
Verification script for scoring transparency, period clock, shot counters, and scoring modal.
Tests:
1. SeriesDailyGameOut schema contains home_shots and away_shots.
2. SeriesScheduleOut schema contains real slate summary fields.
3. TestClient reads series-schedule:
   - Daily games return accurate home_shots, away_shots, current_period, and seconds_remaining.
   - Real slate summary reports exact real games counts (final / live / total).
4. Auto-sync pacing: anchors started_at to real kickoffs and finalizes games when all real games are final.
5. Static files: scoring-modal.js and league.html contain scoring guide buttons and modal markup.

Run with .venv-api:
    .\.venv-api\Scripts\python.exe scripts/verify_scoring_transparency_and_modal.py
"""

import datetime
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Mock external network calls to keep test offline and hermetic
_fetch_patcher = patch("fantasy_sports.platform.nhl_daily_sync._fetch_json", return_value=None)
_fetch_patcher.start()

db_path = Path("scoring_transparency_test.db")
os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"

if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient

from fantasy_sports.api.main import app
from fantasy_sports.api.schemas import SeriesDailyGameOut, SeriesScheduleOut
from fantasy_sports.platform.db.base import Base
from fantasy_sports.platform.db.session import SessionLocal, engine
from fantasy_sports.platform.db.models.core import (
    Game, League, LeagueSeries, LeagueUniverse, RealTeam, SeriesDailyGame,
    SimulatorEvent, SimulatorGame, Team, Universe, Week, User
)
from fantasy_sports.platform import series_schedule
from fantasy_sports.platform.simulator import service, state as st

Base.metadata.create_all(bind=engine)
client = TestClient(app)

def test_schemas():
    print("[1/5] Testing Pydantic schemas...")
    dg_out = SeriesDailyGameOut(
        id=1, series_id=1, calendar_date=datetime.date.today(),
        is_matchup_day=True, home_team_id=1, away_team_id=2,
        home_score=3, away_score=2, home_shots=25, away_shots=22,
        current_period=2, seconds_remaining=640, game_score_rating=65,
        status="LIVE", owner_watched=True
    )
    assert dg_out.home_shots == 25, f"Expected 25 home_shots, got {dg_out.home_shots}"
    assert dg_out.away_shots == 22, f"Expected 22 away_shots, got {dg_out.away_shots}"

    sched_out = SeriesScheduleOut(
        league_id=1, series_length_days=7, series=[],
        real_slate_summary="6 of 12 real NHL games final · 6 live in progress",
        real_games_total=12, real_games_final=6, real_games_live=6
    )
    assert sched_out.real_games_total == 12
    assert sched_out.real_games_final == 6
    assert sched_out.real_games_live == 6
    assert "6 of 12 real NHL games final" in sched_out.real_slate_summary
    print("  -> Schema check passed!")

def test_api_series_schedule_transparency():
    print("[2/5] Testing /leagues/{id}/series-schedule response transparency...")
    db = SessionLocal()
    try:
        nhl = Universe(code="nhl", sport="hockey", level="pro", display_name="National Hockey League")
        db.add(nhl)
        db.flush()

        user = User(email="commish@example.com", display_name="Commish")
        db.add(user)
        db.flush()

        league = League(name="NHL Test League", commissioner_user_id=user.id, series_length_days=7)
        db.add(league)
        db.flush()

        db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id))
        db.flush()

        t1 = Team(league_id=league.id, name="Bruins", owner_user_id=user.id, program_size="large")
        t2 = Team(league_id=league.id, name="Rangers", is_ai_owned=True, program_size="large")
        db.add_all([t1, t2])
        db.flush()

        today = datetime.date.today()
        sunday = today - datetime.timedelta(days=(today.weekday() + 1 if today.weekday() != 6 else 0))
        series_schedule.generate_schedule(db, league, sunday, 1, replace=True, window_rows=False)
        db.flush()

        # Seed real games for today to test slate summary
        rt1 = RealTeam(universe_id=nhl.id, name="Boston Bruins", external_ref="BOS")
        rt2 = RealTeam(universe_id=nhl.id, name="New York Rangers", external_ref="NYR")
        db.add_all([rt1, rt2])
        db.flush()

        week = Week(universe_id=nhl.id, season_year=2026, week_number=1, start_date=sunday, end_date=sunday + datetime.timedelta(days=6))
        db.add(week)
        db.flush()

        now_utc = service.utcnow()
        # Set kickoff 2 hours ago so it reflects live in-game progress
        kickoff_past = now_utc - datetime.timedelta(hours=2)
        start_day = datetime.datetime.combine(today, datetime.time.min)
        rg1 = Game(universe_id=nhl.id, week_id=week.id, home_team_id=rt1.id, away_team_id=rt2.id,
                   home_score=4, away_score=2, kickoff_at=kickoff_past, status="final")
        rg2 = Game(universe_id=nhl.id, week_id=week.id, home_team_id=rt1.id, away_team_id=rt2.id,
                   home_score=2, away_score=1, kickoff_at=kickoff_past, status="live")
        db.add_all([rg1, rg2])
        db.flush()

        # Find today's daily game
        dg = db.query(SeriesDailyGame).filter_by(calendar_date=today).first()
        assert dg is not None

        # Create a simulator game with events
        sim = SimulatorGame(
            daily_game_id=dg.id, contract_version="1.0", sport="hockey", periods=3, period_seconds=1200,
            window_seconds=10800, event_count=4, script_digest="testdigest", status=st.LIVE, started_at=kickoff_past
        )
        db.add(sim)
        db.flush()

        # Events: shots and goals
        ev1 = SimulatorEvent(simulator_game_id=sim.id, seq=1, kind="shot", period=1, game_second=300,
                             team_side="home", home_score=0, away_score=0, home_shots=1, away_shots=0,
                             headline="Shot saved")
        ev2 = SimulatorEvent(simulator_game_id=sim.id, seq=2, kind="goal", period=1, game_second=600,
                             team_side="home", home_score=1, away_score=0, home_shots=2, away_shots=1,
                             headline="Goal scored")
        ev3 = SimulatorEvent(simulator_game_id=sim.id, seq=3, kind="shot", period=2, game_second=1500,
                             team_side="away", home_score=1, away_score=0, home_shots=8, away_shots=10,
                             headline="Wrist shot on goal")
        ev4 = SimulatorEvent(simulator_game_id=sim.id, seq=4, kind="goal", period=2, game_second=1800,
                             team_side="away", home_score=1, away_score=1, home_shots=9, away_shots=11,
                             headline="Power play goal")
        db.add_all([ev1, ev2, ev3, ev4])
        db.commit()

        # Call the GET endpoint
        res = client.get(f"/leagues/{league.id}/series-schedule")
        assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
        data = res.json()

        # Slate summary
        assert data.get("real_games_total") == 2, f"Expected total 2, got {data.get('real_games_total')}"
        assert data.get("real_games_final") == 1, f"Expected final 1, got {data.get('real_games_final')}"
        assert data.get("real_games_live") == 1, f"Expected live 1, got {data.get('real_games_live')}"
        assert "1 of 2 real NHL games final" in data.get("real_slate_summary", "")

        # Daily game details
        series_list = data["series"]
        all_dgs = [g for s in series_list for g in s["daily_games"]]
        today_dg = next(g for g in all_dgs if g["calendar_date"] == today.isoformat())

        assert today_dg["home_shots"] == 9, f"Expected 9 home_shots, got {today_dg['home_shots']}"
        assert today_dg["away_shots"] == 11, f"Expected 11 away_shots, got {today_dg['away_shots']}"
        assert today_dg["home_score"] == 1, f"Expected 1 home_score, got {today_dg['home_score']}"
        assert today_dg["away_score"] == 1, f"Expected 1 away_score, got {today_dg['away_score']}"
        assert today_dg["current_period"] == 2, f"Expected period 2, got {today_dg['current_period']}"
        assert 0 <= today_dg["seconds_remaining"] <= 1200, f"Invalid seconds_remaining: {today_dg['seconds_remaining']}"

        print("  -> Series schedule API transparency verified successfully!")
    finally:
        db.close()

def test_finalization_when_all_real_games_final():
    print("[3/5] Testing auto-sync finalization when all real games are final...")
    db = SessionLocal()
    try:
        from fantasy_sports.platform.nhl_daily_sync import auto_sync_league_scoreboard
        league = db.query(League).first()

        # Update all real games to final
        db.query(Game).update({"status": "final"})
        db.commit()

        # Run auto_sync
        auto_sync_league_scoreboard(db, league.id)

        today = datetime.date.today()
        dg = db.query(SeriesDailyGame).filter_by(calendar_date=today).first()
        sim = service.get_sim(db, dg.id)

        assert sim.status == st.FINAL, f"Expected sim status FINAL, got {sim.status}"
        assert dg.status == "FINAL", f"Expected dg status FINAL, got {dg.status}"
        assert dg.current_period == 3, f"Expected period 3, got {dg.current_period}"
        assert dg.seconds_remaining == 0, f"Expected seconds_remaining 0, got {dg.seconds_remaining}"
        print("  -> Daily game auto-finalized cleanly upon real slate completion!")
    finally:
        db.close()

def test_static_files():
    print("[4/5] Checking static files for ScoringModal and markup...")
    modal_file = Path("web/static/scoring-modal.js")
    assert modal_file.exists(), "web/static/scoring-modal.js does not exist!"
    content = modal_file.read_text(encoding="utf-8")
    assert "const ScoringModal =" in content, "ScoringModal definition missing!"
    assert "sm-tab-nhl" in content, "NHL tab content missing in scoring-modal.js!"
    assert "sm-tab-football" in content, "Football tab content missing in scoring-modal.js!"
    assert "Shots on Goal (SOG)" in content, "Shots explanation missing!"
    assert "Goals Saved Above Expected" in content, "Goaltending explanation missing!"

    league_file = Path("web/static/league.html")
    league_content = league_file.read_text(encoding="utf-8")
    assert "scoring-modal.js" in league_content, "scoring-modal.js not referenced in league.html!"
    assert "btn-how-scoring-works" in league_content, "btn-how-scoring-works missing in league.html!"
    assert "btn-scoring-info-sb" in league_content, "btn-scoring-info-sb missing in league.html!"
    assert "clockSubheader" in league_content, "clockSubheader missing in league.html!"
    assert "SOG:" in league_content, "SOG shot display missing in league.html!"
    assert "0 - 0 in progress" not in league_content, "Hardcoded '0 - 0 in progress' override still present in league.html!"

    commish_file = Path("web/static/commissioner.html")
    commish_content = commish_file.read_text(encoding="utf-8")
    assert "scoring-modal.js" in commish_content, "scoring-modal.js not referenced in commissioner.html!"
    assert "cm-scoring-info-btn" in commish_content, "cm-scoring-info-btn missing in commissioner.html!"
    print("  -> Static files and frontend markup verified successfully!")

def test_cleanup():
    print("[5/5] Cleanup temporary test database...")
    if db_path.exists():
        try:
            db_path.unlink()
        except Exception:
            pass
    print("  -> Cleaned up.")

if __name__ == "__main__":
    test_schemas()
    test_api_series_schedule_transparency()
    test_finalization_when_all_real_games_final()
    test_static_files()
    test_cleanup()
    print("\nALL SCORING TRANSPARENCY AND MODAL TESTS PASSED!")
