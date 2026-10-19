"""
Acquisition limits: an optional cap on how many players a team can acquire in a period. Off by default.

  * leagues.acquisition_limit is the most acquisitions a team may make in one period (NULL = no limit, the default);
    leagues.acquisition_limit_period is the period: day, week, game, series or season.
  * An acquisition is a person's own free-agent signing or a waiver claim they win (FreeAgencyTransaction reason "user" or
    "waiver"). The AI's required fill, commissioner corrections and trades don't count, and AI teams are never limited.
  * Periods run on Pacific time, like waivers:
      day     - the Pacific calendar day.
      week    - the fantasy week, Sunday 00:00 to Saturday night (the waiver reset).
      game    - hockey: the team's current fantasy window game (from the day after its previous game ended to the end of this
                one). Other sports have one game a week, so it is the week.
      series  - hockey: the team's current series (start date to end date). Other sports have no series, so it is the week.
      season  - everything this season (transactions are cleared at rollover).
  * A claim is checked when it is placed and again when it is decided; one that would go over the limit fails with a note.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from fantasy_sports.platform import waivers
from fantasy_sports.platform.db.models.core import FreeAgencyTransaction, League, LeagueSeries, SeriesDailyGame, Team

PERIODS = ("day", "week", "game", "series", "season")
COUNTED_REASONS = ("user", waivers.WAIVER)
MAX_LIMIT = 1000


def limit_of(league: League) -> int | None:
    return league.acquisition_limit


def period_of(league: League) -> str:
    return league.acquisition_limit_period if league.acquisition_limit_period in PERIODS else "week"


def _midnight(day: datetime.date) -> datetime.datetime:
    return waivers._to_utc(datetime.datetime.combine(day, datetime.time()))


def period_start(db: Session, league: League, team: Team, now: datetime.datetime) -> datetime.datetime | None:
    """When the team's current period began (naive UTC), or None when it began with the season."""
    period = period_of(league)
    today = waivers._to_local(now).date()
    if period == "season":
        return None
    if period == "day":
        return _midnight(today)
    if period == "game" and league.series_length_days is not None:
        games = (db.query(SeriesDailyGame).join(LeagueSeries, LeagueSeries.id == SeriesDailyGame.series_id)
                 .filter(LeagueSeries.league_id == league.id,
                         (SeriesDailyGame.home_team_id == team.id) | (SeriesDailyGame.away_team_id == team.id)).all())
        ends = sorted((g.calendar_date_end or g.calendar_date, g.calendar_date) for g in games)
        current = [e for e in ends if e[1] <= today <= e[0]]
        if current:
            return _midnight(current[0][1])
        earlier = [e for e in ends if e[0] < today]
        return _midnight(earlier[-1][0] + datetime.timedelta(days=1)) if earlier else None
    if period == "series" and league.series_length_days is not None:
        starts = [s.start_date for s in db.query(LeagueSeries).filter(
            LeagueSeries.league_id == league.id, LeagueSeries.start_date <= today,
            (LeagueSeries.team_a_id == team.id) | (LeagueSeries.team_b_id == team.id))]
        return _midnight(max(starts)) if starts else None
    return waivers.last_reset(now)


def used(db: Session, team: Team, now: datetime.datetime | None = None) -> int:
    """Acquisitions the team has made in its current period."""
    now = now or datetime.datetime.utcnow()
    league = db.get(League, team.league_id)
    start = period_start(db, league, team, now)
    rows = db.query(FreeAgencyTransaction).filter(
        FreeAgencyTransaction.team_id == team.id, FreeAgencyTransaction.kind == "add",
        FreeAgencyTransaction.reason.in_(COUNTED_REASONS), FreeAgencyTransaction.created_at <= now)
    if start is not None:
        rows = rows.filter(FreeAgencyTransaction.created_at >= start)
    return rows.count()


def problem(db: Session, team: Team, now: datetime.datetime | None = None) -> str | None:
    """Why the team can't acquire another player under the league's limit right now, or None."""
    league = db.get(League, team.league_id)
    limit = limit_of(league)
    if limit is None or team.is_ai_owned:
        return None
    n = used(db, team, now)
    if n >= limit:
        return (f"Your team has reached the league's limit of {limit} acquisition{'' if limit == 1 else 's'} "
                f"per {period_of(league)} ({n} used)")
    return None


def summary(db: Session, team: Team | None, league: League, now: datetime.datetime | None = None) -> dict | None:
    """The limit, its period and (for a team) how many are used and left, or None when the league has no limit."""
    limit = limit_of(league)
    if limit is None:
        return None
    out = {"limit": limit, "period": period_of(league), "used": None, "left": None}
    if team is not None and not team.is_ai_owned:
        out["used"] = used(db, team, now)
        out["left"] = max(0, limit - out["used"])
    return out
