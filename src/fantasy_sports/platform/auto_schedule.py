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


def ensure_schedule(db: Session, league: League, today: datetime.date | None = None) -> str | None:
    """Creates the league's schedule if it has none. Returns None if the league now has one, else why it can't."""
    if is_college(db, league.id):
        return "College leagues arrange their own schedule in the schedule stage"
    if has_schedule(db, league):
        return None
    if today is None:
        try:
            from zoneinfo import ZoneInfo
            # Adjust to user/North American timezone so evening drafts don't prematurely roll over
            today = datetime.datetime.now(ZoneInfo("America/New_York")).date()
        except Exception:
            today = datetime.date.today()
    try:
        with db.begin_nested():            # a failed attempt leaves nothing behind
            if series_schedule.league_is_nhl(db, league.id):
                if getattr(league, "league_mode", "standard") == "weekly_redraft":
                    raise ValueError("This league plays weekly redraft contests, not series")
                from fantasy_sports.sports.hockey.pipeline import week_start
                # NHL fantasy weeks run Sunday to Saturday. The series schedule begins on the Sunday of the fantasy week.
                series_start = week_start(today)
                series_schedule.generate_schedule(db, league, series_start, 1)
            else:
                schedule.create_schedule(db, league.id)
    except ValueError as e:
        return str(e)
    return None
