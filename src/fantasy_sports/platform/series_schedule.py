"""
NHL multi-day series schedule (see docs/data_contract.md, "NHL series schedule").

Deliberately separate from platform/schedule.py, which is the football weekly-matchup stage: nothing
here touches weekly_matchups, weeks, or football rules.

Contract:
  * Teams are paired by the circle method over teams sorted by id, so the schedule is a pure function
    of (team ids, series length, start date, cycles).
  * Each round-robin cycle has N-1 rounds (N even, N >= 2). Every team plays exactly one opponent per
    round, and each round is one series of `series_length_days` consecutive calendar days; rounds run
    back to back with no gap.
  * A series is scored in fantasy WINDOWS (docs/hockey_spec.md section 5): Sun-Tue, Wed-Thu and Fri-Sat. Each
    window is ONE game and one series_daily_games row, dated from the window's first day to its last
    (calendar_date .. calendar_date_end). A 7-day series starting on a Sunday has 3 rows; a series that starts or
    ends mid-window gets a shorter window from the days it has. A team has exactly one game per window.
  * Extra cycles repeat the pairings with team_a/team_b swapped on odd cycles.
  * Within a series team_a hosts on days 1, 3, 5, ... and team_b hosts on days 2, 4, ...
  * Generation is replace-only: it refuses to run over an existing schedule unless replace=True, and
    even then only while nothing has started (every series SCHEDULED, every game SCHEDULED with a
    0-0 score). Rows are never edited in place, so the same inputs always yield the same rows.
  * No real NHL slate is consulted: every day is created with is_matchup_day=True. The column is the
    input a later step uses to mark off days.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class PlannedWindow:
    window_index: int          # 1 Sun-Tue, 2 Wed-Thu, 3 Fri-Sat
    start: datetime.date
    end: datetime.date
    day_index: int             # 0-based day within the series on which the window starts


def plan_windows(series: "PlannedSeries | LeagueSeries") -> list[PlannedWindow]:
    """The fantasy windows a series covers, earliest first. Consecutive days that fall in the same window of the same
    week form one window. Pure."""
    from fantasy_sports.sports.hockey.pipeline import window_of

    out: list[PlannedWindow] = []
    keys: list[tuple] = []
    for d in range(series.total_days):
        day = series.start_date + datetime.timedelta(days=d)
        key = window_of(day)
        if keys and keys[-1] == key:
            last = out[-1]
            out[-1] = PlannedWindow(last.window_index, last.start, day, last.day_index)
        else:
            keys.append(key)
            out.append(PlannedWindow(key[1], day, day, d))
    return out


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


def league_uses_windows(db: Session, league_id: int) -> bool:
    """True for a league in any window sport (NHL, NBA): it plays the series schedule, not weekly matchups."""
    from fantasy_sports.platform.window_sports import league_uses_windows as _uses
    return _uses(db, league_id)


def has_schedule(db: Session, league_id: int) -> bool:
    return db.query(LeagueSeries).filter_by(league_id=league_id).count() > 0


def _started(db: Session, league_id: int) -> bool:
    if db.query(LeagueSeries).filter(LeagueSeries.league_id == league_id, LeagueSeries.status != "SCHEDULED").count():
        return True
    if db.query(SeriesDailyGame).join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id).filter(
        LeagueSeries.league_id == league_id).filter(
        (SeriesDailyGame.status != "SCHEDULED") | (SeriesDailyGame.home_score != 0) | (SeriesDailyGame.away_score != 0)).count() > 0:
        return True
    from fantasy_sports.platform.db.models.core import SimulatorGame
    return db.query(SimulatorGame).join(SeriesDailyGame, SeriesDailyGame.id == SimulatorGame.daily_game_id).join(
        LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id).filter(
        LeagueSeries.league_id == league_id, SimulatorGame.status != "SCHEDULED").count() > 0


def _discard_sims(db: Session, dg_ids: list[int]) -> None:
    """Delete the simulations (events, viewer reveals) of the given game rows."""
    from fantasy_sports.platform.db.models.core import SimulatorEvent, SimulatorGame, SimulatorReveal
    sim_ids = [s[0] for s in db.query(SimulatorGame.id).filter(SimulatorGame.daily_game_id.in_(dg_ids)).all()]
    if sim_ids:
        db.query(SimulatorReveal).filter(SimulatorReveal.simulator_game_id.in_(sim_ids)).delete(synchronize_session=False)
        db.query(SimulatorEvent).filter(SimulatorEvent.simulator_game_id.in_(sim_ids)).delete(synchronize_session=False)
        db.query(SimulatorGame).filter(SimulatorGame.id.in_(sim_ids)).delete(synchronize_session=False)


def _discard_games(db: Session, dg_ids: list[int]) -> None:
    """Delete game rows with their simulations, reveals and banking links."""
    from fantasy_sports.platform.db.models.core import BankedCredit
    _discard_sims(db, dg_ids)
    db.query(BankedCredit).filter(BankedCredit.assigned_daily_game_id.in_(dg_ids)).update({"assigned_daily_game_id": None}, synchronize_session=False)
    db.query(SeriesDailyGame).filter(SeriesDailyGame.id.in_(dg_ids)).delete(synchronize_session=False)


def convert_to_window_rows(db: Session, league_id: int) -> dict:
    """Move an existing one-row-per-day schedule to window rows (docs/hockey_spec.md section 5). Idempotent.

    For each window of each series: if none of its daily rows is FINAL, the rows collapse into ONE window row (the
    earliest day's row, now spanning the window) and every simulation of those rows is discarded, so the window is
    scored again from scratch as one game. A window with any FINAL daily row is left exactly as played - its days
    stay individual games - so no result is ever rewritten. Flushes, does not commit."""
    converted = kept_as_played = 0
    for s in db.query(LeagueSeries).filter_by(league_id=league_id).order_by(LeagueSeries.id).all():
        legacy = sorted((g for g in s.daily_games if g.window_index is None), key=lambda g: g.calendar_date)
        if not legacy:
            continue
        for w in plan_windows(s):
            group = [g for g in legacy if w.start <= g.calendar_date <= w.end]
            if not group:
                continue
            if any(g.status == "FINAL" for g in group):
                kept_as_played += 1
                continue
            keep, rest = group[0], group[1:]
            if rest:
                _discard_games(db, [g.id for g in rest])
            _discard_sims(db, [keep.id])
            keep.calendar_date_end, keep.window_index = w.end, w.window_index
            keep.status, keep.home_score, keep.away_score = "SCHEDULED", 0, 0
            keep.current_period, keep.seconds_remaining = 0, 1200
            converted += 1
        db.flush()
        db.expire(s, ["daily_games"])
    return {"converted_windows": converted, "left_as_played": kept_as_played}


def generate_schedule(db: Session, league: League, start_date: datetime.date, cycles: int = 1, replace: bool = False, force: bool = False,
                      window_rows: bool = True) -> list[LeagueSeries]:
    """Create the series and game rows: one row per fantasy window (the default), or one per calendar day with
    window_rows=False (the legacy shape; off-day banking still works on those). Flushes but does not commit.
    Raises ValueError."""
    if not league_uses_windows(db, league.id) or league.series_length_days is None:
        raise ValueError("Series schedules are only available for NHL and NBA leagues")
    if getattr(league, "league_mode", "standard") == "weekly_redraft":
        raise ValueError("This league plays weekly redraft contests, not series")
    team_ids = [t.id for t in db.query(Team).filter_by(league_id=league.id)]
    plan = plan_series(team_ids, league.series_length_days, start_date, cycles)
    if has_schedule(db, league.id):
        if not replace:
            raise ValueError("This league already has a series schedule; pass replace=true to regenerate it")
        if _started(db, league.id) and not force:
            raise ValueError("The schedule can't be replaced once any series or game has started; pass force=true to override")
        series_ids = [s[0] for s in db.query(LeagueSeries.id).filter_by(league_id=league.id).all()]
        dg_ids = [dg[0] for dg in db.query(SeriesDailyGame.id).filter(SeriesDailyGame.series_id.in_(series_ids)).all()]
        if dg_ids:
            _discard_games(db, dg_ids)
        db.query(LeagueSeries).filter_by(league_id=league.id).delete(synchronize_session=False)
        db.flush()
    rows = []
    for p in plan:
        s = LeagueSeries(league_id=league.id, team_a_id=p.team_a_id, team_b_id=p.team_b_id, start_date=p.start_date,
                         end_date=p.end_date, total_days=p.total_days, status="SCHEDULED")
        db.add(s)
        db.flush()
        if not window_rows:
            for d in range(p.total_days):
                home, away = daily_home_away(p, d)
                db.add(SeriesDailyGame(series_id=s.id, calendar_date=p.start_date + datetime.timedelta(days=d),
                                       is_matchup_day=True, home_team_id=home, away_team_id=away, status="SCHEDULED"))
        for ordinal, w in enumerate(plan_windows(p) if window_rows else []):
            home, away = daily_home_away(p, ordinal)       # team_a hosts the 1st, 3rd, ... window of the series
            db.add(SeriesDailyGame(series_id=s.id, calendar_date=w.start, calendar_date_end=w.end, window_index=w.window_index,
                                   is_matchup_day=True, home_team_id=home, away_team_id=away, status="SCHEDULED"))
        rows.append(s)
    db.flush()
    return rows
