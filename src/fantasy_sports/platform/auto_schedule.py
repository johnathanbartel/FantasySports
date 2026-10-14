"""Schedules that make themselves, for every league that doesn't let teams set their own (everything but college).

  * NHL: the multi-day series round robin (platform/series_schedule.py), starting the day after the draft.
  * NFL: the weekly round-robin matchups (platform/schedule.py) over the season's calendar weeks.

Called when a snake draft completes, and on demand by the commissioner. Never raises for "can't yet" cases (no calendar
weeks ingested, an odd number of teams...): it returns the reason so a page can show it.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from fantasy_sports.platform import schedule, series_schedule
from fantasy_sports.platform.db.models.core import League, WeeklyMatchup
from fantasy_sports.platform.league_features import is_college


def has_schedule(db: Session, league: League) -> bool:
    if series_schedule.league_is_nhl(db, league.id):
        return series_schedule.has_schedule(db, league.id)
    return db.query(WeeklyMatchup).filter_by(league_id=league.id).count() > 0


def next_full_window_start(d: datetime.date) -> datetime.date:
    """The start date of the next full window strictly after `d` (Wed, Fri, or Sun)."""
    w = d.weekday()  # Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    if w in (6, 0, 1):  # Sun, Mon, Tue -> next full window is Wed (2)
        days = (2 - w) % 7
        return d + datetime.timedelta(days=days if days > 0 else 7)
    elif w in (2, 3):  # Wed, Thu -> next full window is Fri (4)
        days = (4 - w) % 7
        return d + datetime.timedelta(days=days if days > 0 else 7)
    else:  # Fri (4), Sat (5) -> next full window is Sun (6)
        days = (6 - w) % 7
        return d + datetime.timedelta(days=days if days > 0 else 7)


def reschedule_nhl_league(db: Session, league: League, today: datetime.date | None = None, force: bool = False) -> str | None:
    """Reschedules an NHL league so its series start at the beginning of the next full window."""
    if not series_schedule.league_is_nhl(db, league.id):
        return "Not an NHL league"
    if today is None:
        try:
            from fantasy_sports.platform.nhl_daily_sync import to_eastern_naive
            today = to_eastern_naive(datetime.datetime.now(datetime.timezone.utc)).date()
        except Exception:
            today = datetime.date.today()
    series_start = next_full_window_start(today)
    series_schedule.generate_schedule(db, league, series_start, 1, replace=True, force=force)
    return None


def ensure_schedule(db: Session, league: League, today: datetime.date | None = None) -> str | None:
    """Creates the league's schedule if it has none. Returns None if the league now has one, else why it can't."""
    if is_college(db, league.id):
        return "College leagues arrange their own schedule in the schedule stage"
    if today is None:
        try:
            from fantasy_sports.platform.nhl_daily_sync import to_eastern_naive
            today = to_eastern_naive(datetime.datetime.now(datetime.timezone.utc)).date()
        except Exception:
            today = datetime.date.today()
    if has_schedule(db, league):
        if series_schedule.league_is_nhl(db, league.id):
            from fantasy_sports.platform.db.models.core import LeagueSeries
            first_s = db.query(LeagueSeries).filter_by(league_id=league.id).order_by(LeagueSeries.start_date).first()
            if first_s and first_s.start_date < today:
                # League drafted after season started; heal schedule if no human watched games
                target_start = next_full_window_start(today)
                if first_s.start_date < target_start:
                    try:
                        with db.begin_nested():
                            series_schedule.generate_schedule(db, league, target_start, 1, replace=True, force=True)
                    except Exception:
                        pass
        return None
    try:
        with db.begin_nested():            # a failed attempt leaves nothing behind
            if series_schedule.league_is_nhl(db, league.id):
                if getattr(league, "league_mode", "standard") == "weekly_redraft":
                    raise ValueError("This league plays weekly redraft contests, not series")
                # NHL leagues begin their season at the beginning of the next full window (Wed, Fri, or Sun)
                series_start = next_full_window_start(today)
                series_schedule.generate_schedule(db, league, series_start, 1)
            else:
                schedule.create_schedule(db, league.id)
    except ValueError as e:
        return str(e)
    return None

