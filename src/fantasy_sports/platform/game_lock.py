"""
The game-start lock: once a week's first real game starts, nobody can move players - no signing, dropping, waiver
claims, trades of players, or lineup changes - until the week is over. Every sport uses the same rule.

The lock moment is the earliest kickoff of the week's games, as an exact UTC instant. Kickoffs are stored in more
than one way, so they are normalised first (see kickoff_utc). A week with no games loaded locks at the start of its
first day (US Eastern), the same as the older date rule.

Known limit: NFL kickoffs are stored as a date with no time of day, so an NFL week locks at midnight Eastern on its
first game day, not at the first kickoff.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import Game, LeagueUniverse, Universe, Week

LOCKED_MESSAGE = "Games have started - rosters and lineups are locked until the week ends"


def _nth_sunday(year: int, month: int, n: int) -> datetime.date:
    first = datetime.date(year, month, 1)
    return first + datetime.timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def eastern_offset(naive_local: datetime.datetime) -> datetime.timedelta:
    """UTC minus Eastern for a wall-clock Eastern time, by the US rule (DST: 2nd Sunday of March to 1st of November).
    Hand-rolled because the zone database isn't installed everywhere this runs."""
    y = naive_local.year
    dst_start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(2))
    dst_end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(2))
    return datetime.timedelta(hours=4 if dst_start <= naive_local < dst_end else 5)


def eastern_to_utc(naive_local: datetime.datetime) -> datetime.datetime:
    return naive_local + eastern_offset(naive_local)


def utc_to_eastern(naive_utc: datetime.datetime) -> datetime.datetime:
    local = naive_utc - datetime.timedelta(hours=5)
    return naive_utc - eastern_offset(local)


def kickoff_utc(game: Game, universe_code: str | None) -> datetime.datetime | None:
    """A game's kickoff as naive UTC. College rows are UTC already. NHL rows are Eastern wall-clock time, except that
    some were stored as UTC (no NHL game starts before 5am Eastern, so an hour under 5 gives those away). A kickoff
    at exactly midnight has no time of day (NFL): it counts from the start of that day, Eastern."""
    k = game.kickoff_at
    if k is None:
        return None
    if universe_code == "nhl" and k.hour >= 5:
        return eastern_to_utc(k)
    if k.time() == datetime.time(0) and universe_code != "nhl":
        return eastern_to_utc(k)
    return k


def week_lock_at(db: Session, week: Week) -> datetime.datetime | None:
    """When the week locks (naive UTC): its earliest kickoff, else the start of its first day."""
    universe = db.get(Universe, week.universe_id)
    code = universe.code if universe else None
    kicks = [kickoff_utc(g, code) for g in db.query(Game).filter_by(week_id=week.id)]
    kicks = [k for k in kicks if k is not None]
    if kicks:
        return min(kicks)
    if week.start_date is not None:
        return eastern_to_utc(datetime.datetime.combine(week.start_date, datetime.time(0)))
    return None


def week_over(week: Week, now: datetime.datetime) -> bool:
    """The week's last day has ended (Eastern)."""
    end = week.end_date or (week.start_date + datetime.timedelta(days=6) if week.start_date else None)
    return end is not None and utc_to_eastern(now).date() > end


def is_week_locked(db: Session, week: Week, now: datetime.datetime | None = None) -> bool:
    """True from the week's first kickoff until its last day ends. Past weeks stay locked, as lineups always were."""
    now = now or datetime.datetime.utcnow()
    lock = week_lock_at(db, week)
    return lock is not None and now >= lock


def locked_week(db: Session, league_id: int, now: datetime.datetime | None = None) -> Week | None:
    """The league's week that is under way (started, not yet over), or None when roster moves are open."""
    now = now or datetime.datetime.utcnow()
    for lu in db.query(LeagueUniverse).filter_by(league_id=league_id):
        for week in db.query(Week).filter_by(universe_id=lu.universe_id):
            if is_week_locked(db, week, now) and not week_over(week, now):
                return week
    return None


def assert_moves_open(db: Session, league_id: int, now: datetime.datetime | None = None) -> None:
    """Raises ValueError (a message meant for the user) when games are under way and players can't be moved."""
    if locked_week(db, league_id, now) is not None:
        raise ValueError(LOCKED_MESSAGE)
