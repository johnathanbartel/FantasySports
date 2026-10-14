"""
NHL multi-day series schedule (see docs/data_contract.md, "NHL series schedule").

Deliberately separate from platform/schedule.py, which is the football weekly-matchup stage: nothing
here touches weekly_matchups, weeks, or football rules.

Contract:
  * Teams are paired by the circle method over teams sorted by id, so the schedule is a pure function
    of (team ids, series length, start date, cycles).
  * Each round-robin cycle has N-1 rounds (N even, N >= 2). Every team plays exactly one opponent per
    round, and each round is one series of `series_length_days` consecutive calendar days; rounds run
    back to back with no gap. A team therefore has exactly one game on every date of the schedule.
  * Extra cycles repeat the pairings with team_a/team_b swapped on odd cycles.
  * Within a series team_a hosts on days 1, 3, 5, ... and team_b hosts on days 2, 4, ...
  * Generation is replace-only: it refuses to run over an existing schedule unless replace=True, and
    even then only while nothing has started (every series SCHEDULED, every game SCHEDULED with a
    0-0 score). Rows are never edited in place, so the same inputs always yield the same rows.
  * No real NHL slate is consulted: every day is created with is_matchup_day=True. The column is the
    input a later step uses to mark off days.
"""

import datetime
from dataclasses import dataclass

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import (
    League, LeagueSeries, LeagueUniverse, SeriesDailyGame, Team, Universe,
)

ALLOWED_SERIES_LENGTHS = (5, 7, 10)
MAX_CYCLES = 4


@dataclass(frozen=True)
class PlannedSeries:
    round_index: int
    team_a_id: int
    team_b_id: int
    start_date: datetime.date
    end_date: datetime.date
    total_days: int


def plan_series(team_ids: list[int], series_length_days: int, start_date: datetime.date, cycles: int = 1) -> list[PlannedSeries]:
    """Pure round-robin planner; raises ValueError for unsupported shapes."""
    if series_length_days not in ALLOWED_SERIES_LENGTHS:
        raise ValueError("Series length must be 5, 7, or 10 days")
    if not 1 <= cycles <= MAX_CYCLES:
        raise ValueError(f"cycles must be between 1 and {MAX_CYCLES}")
    ids = sorted(team_ids)
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate team ids")
    n = len(ids)
    if n < 2 or n % 2:
        raise ValueError(f"A series schedule needs an even number of teams (at least 2); this league has {n}")
    plan: list[PlannedSeries] = []
    rotating = ids[1:]
    for cycle in range(cycles):
        for rnd in range(n - 1):
            order = [ids[0]] + rotating[-rnd:] + rotating[:-rnd] if rnd else list(ids)
            round_index = cycle * (n - 1) + rnd
            start = start_date + datetime.timedelta(days=round_index * series_length_days)
            end = start + datetime.timedelta(days=series_length_days - 1)
            for i in range(n // 2):
                a, b = order[i], order[n - 1 - i]
                if (rnd + i + cycle) % 2:
                    a, b = b, a
                plan.append(PlannedSeries(round_index, a, b, start, end, series_length_days))
    return plan


def daily_home_away(series: PlannedSeries | LeagueSeries, day_index: int) -> tuple[int, int]:
    """(home_team_id, away_team_id) for the 0-based day within a series."""
    if day_index % 2 == 0:
        return series.team_a_id, series.team_b_id
    return series.team_b_id, series.team_a_id


def league_is_nhl(db: Session, league_id: int) -> bool:
    return db.query(LeagueUniverse).join(Universe, Universe.id == LeagueUniverse.universe_id).filter(
        LeagueUniverse.league_id == league_id, Universe.code == "nhl").count() > 0


def has_schedule(db: Session, league_id: int) -> bool:
    return db.query(LeagueSeries).filter_by(league_id=league_id).count() > 0


def _started(db: Session, league_id: int) -> bool:
    if db.query(LeagueSeries).filter(LeagueSeries.league_id == league_id, LeagueSeries.status != "SCHEDULED").count():
        return True
    return db.query(SeriesDailyGame).join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id).filter(
        LeagueSeries.league_id == league_id).filter(
        (SeriesDailyGame.status != "SCHEDULED") | (SeriesDailyGame.home_score != 0) | (SeriesDailyGame.away_score != 0)).count() > 0


def generate_schedule(db: Session, league: League, start_date: datetime.date, cycles: int = 1, replace: bool = False, force: bool = False) -> list[LeagueSeries]:
    """Create the series and daily-game rows. Flushes but does not commit. Raises ValueError."""
    if not league_is_nhl(db, league.id) or league.series_length_days is None:
        raise ValueError("Series schedules are only available for NHL leagues")
    if getattr(league, "league_mode", "standard") == "weekly_redraft":
        raise ValueError("This league plays weekly redraft contests, not series")
    team_ids = [t.id for t in db.query(Team).filter_by(league_id=league.id)]
    plan = plan_series(team_ids, league.series_length_days, start_date, cycles)
    if has_schedule(db, league.id):
        if not replace:
            raise ValueError("This league already has a series schedule; pass replace=true to regenerate it")
        if _started(db, league.id) and not force:
            raise ValueError("The schedule can't be replaced once any series or game has started; pass force=true to override")
        series_ids = [s.id for s in db.query(LeagueSeries.id).filter_by(league_id=league.id).all()]
        dg_ids = [dg.id for dg in db.query(SeriesDailyGame.id).filter(SeriesDailyGame.series_id.in_(series_ids)).all()]
        if dg_ids:
            from fantasy_sports.platform.db.models.core import SimulatorGame, SimulatorEvent, SimulatorViewer, BankedCredit
            sim_ids = [s.id for s in db.query(SimulatorGame.id).filter(SimulatorGame.daily_game_id.in_(dg_ids)).all()]
            if sim_ids:
                db.query(SimulatorViewer).filter(SimulatorViewer.simulator_game_id.in_(sim_ids)).delete(synchronize_session=False)
                db.query(SimulatorEvent).filter(SimulatorEvent.simulator_game_id.in_(sim_ids)).delete(synchronize_session=False)
                db.query(SimulatorGame).filter(SimulatorGame.id.in_(sim_ids)).delete(synchronize_session=False)
            db.query(BankedCredit).filter(BankedCredit.assigned_daily_game_id.in_(dg_ids)).update({"assigned_daily_game_id": None}, synchronize_session=False)
            db.query(SeriesDailyGame).filter(SeriesDailyGame.id.in_(dg_ids)).delete(synchronize_session=False)
        db.query(LeagueSeries).filter_by(league_id=league.id).delete(synchronize_session=False)
        db.flush()
    rows = []
    for p in plan:
        s = LeagueSeries(league_id=league.id, team_a_id=p.team_a_id, team_b_id=p.team_b_id, start_date=p.start_date,
                         end_date=p.end_date, total_days=p.total_days, status="SCHEDULED")
        db.add(s)
        db.flush()
        for d in range(p.total_days):
            home, away = daily_home_away(p, d)
            db.add(SeriesDailyGame(series_id=s.id, calendar_date=p.start_date + datetime.timedelta(days=d),
                                   is_matchup_day=True, home_team_id=home, away_team_id=away, status="SCHEDULED"))
        rows.append(s)
    db.flush()
    return rows
