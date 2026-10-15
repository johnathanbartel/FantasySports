"""
Offline audit of NHL window rows (docs/hockey_spec.md sections 5 and 10.1): a series is scored in fantasy windows
(Sun-Tue, Wed-Thu, Fri-Sat), one series_daily_games row per window instead of one per calendar day.

  A. plan_windows (pure): which windows a series covers, including series that start or end mid-window.
  B. generate_schedule on a throwaway database: 3 rows per 7-day series, correct dates, host alternation, and the
     legacy one-row-per-day shape still available.
  C. convert_to_window_rows: unplayed windows collapse into one row and their simulations are discarded; a window with
     a FINAL day is left exactly as played; the conversion is idempotent.
  D. Standings and Game Scores count one game per window row.

Exits 0 only if every check passes. From the repository root:

    .venv-api/Scripts/python.exe src/fantasy_sports/sports/hockey/scripts/run_window_rows_audit.py
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / "src"))

D = datetime.date
SUN = D(2026, 10, 11)


def check(condition: bool, message: str) -> None:
    print(("ok    " if condition else "FAIL  ") + message)
    if not condition:
        sys.exit(1)


def audit_plan() -> None:
    from fantasy_sports.platform.series_schedule import PlannedSeries, plan_windows

    def windows(start, days):
        ps = PlannedSeries(0, 1, 2, start, start + datetime.timedelta(days=days - 1), days)
        return [(w.window_index, w.start, w.end, w.day_index) for w in plan_windows(ps)]

    day = datetime.timedelta
    check(windows(SUN, 7) == [(1, SUN, SUN + day(2), 0), (2, SUN + day(3), SUN + day(4), 3), (3, SUN + day(5), SUN + day(6), 5)],
          "a 7-day series from Sunday is three windows: Sun-Tue, Wed-Thu, Fri-Sat")
    wed = SUN + day(3)
    check(windows(wed, 7) == [(2, wed, wed + day(1), 0), (3, wed + day(2), wed + day(3), 2), (1, wed + day(4), wed + day(6), 4)],
          "a 7-day series from Wednesday: Wed-Thu, Fri-Sat, then the next week's Sun-Tue")
    five = windows(SUN, 5)
    check([w[0] for w in five] == [1, 2] and five[-1][2] == SUN + day(4), "a 5-day series from Sunday is Sun-Tue and Wed-Thu")
    ten = windows(SUN, 10)
    check([w[0] for w in ten] == [1, 2, 3, 1] and ten[3][1] == SUN + day(7) and ten[3][2] == SUN + day(9),
          "a 10-day series runs into the next week's Window 1 (a full Sun-Tue)")
    check(all(a[2] < b[1] for a, b in zip(ten, ten[1:])), "windows never overlap and cover each day once")
    covered = sum((w[2] - w[1]).days + 1 for w in ten)
    check(covered == 10, "every day of the series belongs to exactly one window")


def setup(db, teams: int = 4, length: int = 7):
    from fantasy_sports.platform.db.models.core import League, LeagueUniverse, Team, Universe, User

    commissioner = User(email="c@example.com", display_name="C")
    nhl = Universe(sport="hockey", level="pro", code="nhl", display_name="NHL", supports_individual_defense=False)
    db.add_all([commissioner, nhl])
    db.flush()
    league = League(name="WindowRows", commissioner_user_id=commissioner.id, size=teams, series_length_days=length)
    db.add(league)
    db.flush()
    db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id))
    ts = [Team(league_id=league.id, name=f"T{i + 1}", is_ai_owned=True) for i in range(teams)]
    db.add_all(ts)
    db.flush()
    return league, ts


def audit_database() -> None:
    with tempfile.TemporaryDirectory(prefix="fs-winrows-", ignore_cleanup_errors=True) as tmp:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(tmp) / 'rows').as_posix()}"
        from fantasy_sports.platform import series_schedule as ss
        from fantasy_sports.platform import series_standings as sst
        from fantasy_sports.platform.db.base import Base
        from fantasy_sports.platform.db.models.core import LeagueSeries, SeriesDailyGame, SimulatorGame
        from fantasy_sports.platform.db.session import SessionLocal, engine

        Base.metadata.create_all(bind=engine)
        with SessionLocal() as db:
            league, teams = setup(db)
            series = ss.generate_schedule(db, league, SUN, cycles=1)
            db.commit()
            rows = db.query(SeriesDailyGame).order_by(SeriesDailyGame.series_id, SeriesDailyGame.calendar_date).all()
            check(len(series) == 6 and len(rows) == 6 * 3, "4 teams, one cycle: 6 series of 3 window rows each (18 rows, not 42)")
            first = [r for r in rows if r.series_id == series[0].id]
            check([(r.window_index, r.calendar_date, r.calendar_date_end) for r in first] ==
                  [(1, SUN, SUN + datetime.timedelta(days=2)), (2, SUN + datetime.timedelta(days=3), SUN + datetime.timedelta(days=4)),
                   (3, SUN + datetime.timedelta(days=5), SUN + datetime.timedelta(days=6))],
                  "each row carries its window index and its first and last day")
            check(first[0].home_team_id == series[0].team_a_id and first[1].home_team_id == series[0].team_b_id
                  and first[2].home_team_id == series[0].team_a_id, "teams host alternate windows")
            check(all(r.is_matchup_day for r in rows), "every window row is a matchup")

            league2, _ = setup_second(db)
            legacy = ss.generate_schedule(db, league2, SUN, window_rows=False)
            db.commit()
            lrows = db.query(SeriesDailyGame).filter(SeriesDailyGame.series_id.in_([s.id for s in legacy])).all()
            check(len(lrows) == len(legacy) * 7 and all(r.window_index is None and r.calendar_date_end is None for r in lrows),
                  "the legacy shape (one row per day, no window columns) is still available")

            # ---- C. conversion of an existing one-row-per-day schedule ------------------------------------------------------
            s0 = legacy[0]
            days = sorted((r for r in lrows if r.series_id == s0.id), key=lambda r: r.calendar_date)
            days[0].status = "FINAL"                                   # Sunday played: Window 1 stays as played
            days[0].home_score, days[0].away_score = 3, 2
            days[3].status, days[3].home_score = "LIVE_SIMULATING", 1  # Wednesday is live: Window 2 will be rebuilt
            db.add(SimulatorGame(daily_game_id=days[3].id, contract_version="1.0", sport="hockey", periods=3, period_seconds=1200,
                                 window_seconds=10800, event_count=3, script_digest="x" * 64, provenance={}, status="LIVE",
                                 started_at=datetime.datetime(2026, 10, 14, 23, 0)))
            db.commit()
            result = ss.convert_to_window_rows(db, league2.id)
            db.commit()
            after = sorted((r for r in db.query(SeriesDailyGame).filter_by(series_id=s0.id)), key=lambda r: r.calendar_date)
            check(result["left_as_played"] >= 1 and result["converted_windows"] >= 2, f"conversion result: {result}")
            check([r.calendar_date for r in after[:3]] == [SUN, SUN + datetime.timedelta(days=1), SUN + datetime.timedelta(days=2)]
                  and all(r.window_index is None for r in after[:3]) and after[0].status == "FINAL" and after[0].home_score == 3,
                  "Window 1 had a FINAL day: its three daily games are left exactly as played")
            w2 = [r for r in after if r.window_index == 2]
            check(len(w2) == 1 and w2[0].calendar_date == SUN + datetime.timedelta(days=3)
                  and w2[0].calendar_date_end == SUN + datetime.timedelta(days=4) and w2[0].status == "SCHEDULED" and w2[0].home_score == 0,
                  "Window 2 (Wed live, Thu scheduled) collapsed into one reset row spanning Wed-Thu")
            check(db.query(SimulatorGame).filter_by(daily_game_id=w2[0].id).count() == 0, "the live simulation of the collapsed window was discarded")
            check(len([r for r in after if r.window_index == 3]) == 1, "Window 3 (unplayed Fri, Sat) collapsed into one row")
            again = ss.convert_to_window_rows(db, league2.id)
            check(again["converted_windows"] == 0, "converting again changes nothing (idempotent)")
            other = sorted((r for r in db.query(SeriesDailyGame).filter_by(series_id=legacy[1].id)), key=lambda r: r.calendar_date)
            check(all(r.window_index in (1, 2, 3) for r in other) and len(other) == 3, "a series with nothing played became three window rows")

            # ---- D. standings and Game Scores ---------------------------------------------------------------------------------
            sid = series[0].id
            for r in db.query(SeriesDailyGame).filter_by(series_id=sid):
                r.status, r.home_score, r.away_score = "FINAL", 3, 1
            db.commit()
            team_ids, srecs, games = sst.load_league(db, league.id)
            rec = next(s for s in srecs if s.series_id == sid)
            decided = sst.decided_series(srecs, games)
            check(rec.expected_games == 3 and sid in decided, "a series is decided once its 3 window games are FINAL")
            check(sid in decided and decided[sid][0] + decided[sid][1] == 3, "its result is a tally of 3 window games")
            standings = {s.team_id: s for s in sst.compute_standings(team_ids, srecs, games)}
            check(sum(s.wins for s in standings.values()) == 3 and sum(s.losses for s in standings.values()) == 3,
                  "standings count one game per window: 3 wins and 3 losses from the 3 finished rows")


def setup_second(db):
    from fantasy_sports.platform.db.models.core import League, LeagueUniverse, Team, Universe, User

    nhl = db.query(Universe).filter_by(code="nhl").one()
    commissioner = db.query(User).first()
    league = League(name="Legacy", commissioner_user_id=commissioner.id, size=4, series_length_days=7)
    db.add(league)
    db.flush()
    db.add(LeagueUniverse(league_id=league.id, universe_id=nhl.id))
    ts = [Team(league_id=league.id, name=f"L{i + 1}", is_ai_owned=True) for i in range(4)]
    db.add_all(ts)
    db.flush()
    return league, ts


def main() -> None:
    print("A. window planning")
    audit_plan()
    print("B-D. schedule, conversion, standings (throwaway database)")
    audit_database()
    print("\nAll window row audits passed.")


if __name__ == "__main__":
    main()
