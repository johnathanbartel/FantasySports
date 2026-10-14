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


def window_start_date_of(d: datetime.date) -> datetime.date:
    """The calendar start date of the window containing date `d`:
    Window 1 (Sun-Tue): Sunday
    Window 2 (Wed-Thu): Wednesday
    Window 3 (Fri-Sat): Friday
    """
    w = d.weekday()  # Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    if w in (6, 0, 1):  # Sun, Mon, Tue -> Sunday
        days_back = (w + 1) % 7
        return d - datetime.timedelta(days=days_back)
    elif w in (2, 3):  # Wed, Thu -> Wednesday
        return d - datetime.timedelta(days=(w - 2))
    else:  # Fri, Sat -> Friday
        return d - datetime.timedelta(days=(w - 4))


def next_window_start(d: datetime.date) -> datetime.date:
    """The start date of the window strictly following date `d`'s window."""
    w = d.weekday()
    if w in (6, 0, 1):  # Sun, Mon, Tue (Window 1) -> next is Wed (Window 2)
        days = (2 - w) % 7
        return d + datetime.timedelta(days=days if days > 0 else 7)
    elif w in (2, 3):  # Wed, Thu (Window 2) -> next is Fri (Window 3)
        days = (4 - w) % 7
        return d + datetime.timedelta(days=days if days > 0 else 7)
    else:  # Fri, Sat (Window 3) -> next is Sun (Window 1 of next week)
        days = (6 - w) % 7
        return d + datetime.timedelta(days=days if days > 0 else 7)


def first_nhl_puck_drop(db: Session, date: datetime.date) -> datetime.datetime:
    """Returns the kickoff_at datetime of the earliest NHL game on `date`, or 7:00 PM ET if not in DB."""
    try:
        from fantasy_sports.platform.db.models.core import Game, Universe
        nhl = db.query(Universe).filter_by(code="nhl").first()
        if nhl:
            start_dt = datetime.datetime.combine(date, datetime.time(0, 0))
            end_dt = datetime.datetime.combine(date + datetime.timedelta(days=1), datetime.time(0, 0))
            first_game = db.query(Game).filter(
                Game.universe_id == nhl.id,
                Game.kickoff_at >= start_dt,
                Game.kickoff_at < end_dt,
            ).order_by(Game.kickoff_at).first()
            if first_game and first_game.kickoff_at:
                from fantasy_sports.platform.nhl_daily_sync import to_eastern_naive
                return to_eastern_naive(first_game.kickoff_at) if first_game.kickoff_at.hour < 5 else first_game.kickoff_at
    except Exception:
        pass
    # Standard NHL evening slate begins at 7:00 PM Eastern
    return datetime.datetime.combine(date, datetime.time(19, 0))


def calculate_nhl_series_start(db: Session, completed_at: datetime.datetime | None, today: datetime.date) -> datetime.date:
    """Calculates the series start date based on user completion time and NHL game schedule:
    - Calculates when the draft ended based on North American user time (Eastern / local timezone).
    - If the draft completed before the first NHL game of the window, the window is NOT skipped.
    - If it completed during or after the first NHL game, matchups start at the beginning of the next full window.
    """
    from fantasy_sports.platform.nhl_daily_sync import to_eastern_naive
    if completed_at:
        completed_et = to_eastern_naive(completed_at)
        completed_d = completed_et.date()
        win_start = window_start_date_of(completed_d)
        puck_drop = first_nhl_puck_drop(db, win_start)

        if completed_et < puck_drop:
            # Completed before first NHL game of this window: window is not skipped!
            target = win_start
        else:
            # Completed after first NHL game: advance to the next full window
            target = next_window_start(completed_d)

        today_win_start = window_start_date_of(today)
        return max(target, today_win_start)
    else:
        today_win_start = window_start_date_of(today)
        puck_drop = first_nhl_puck_drop(db, today_win_start)
        now_et = to_eastern_naive(datetime.datetime.now(datetime.timezone.utc))
        if now_et < puck_drop:
            return today_win_start
        return next_window_start(today)


def next_full_window_start(d: datetime.date, allow_today_if_window_start: bool = False) -> datetime.date:
    """The start date of the next full window strictly after `d` (Wed, Fri, or Sun).
    If allow_today_if_window_start is True and `d` is the first day of a window (Wed, Fri, or Sun), returns `d`.
    """
    w = d.weekday()  # Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    if allow_today_if_window_start and w in (2, 4, 6):
        return d
    return next_window_start(d)


def reschedule_nhl_league(db: Session, league: League, today: datetime.date | None = None, force: bool = False) -> str | None:
    """Reschedules an NHL league so its series start at the beginning of the next full window."""
    if not series_schedule.league_is_nhl(db, league.id):
        return "Not an NHL league"
    if today is None:
        try:
            from fantasy_sports.platform.nhl_daily_sync import current_eastern_date
            today = current_eastern_date()
        except Exception:
            today = datetime.date.today()
    from fantasy_sports.platform.db.models.core import Draft, DraftPick
    draft = db.query(Draft).filter_by(league_id=league.id).order_by(Draft.id.desc()).first()
    completed_at = draft.completed_at if draft else None
    if draft and not completed_at:
        last_pick = db.query(DraftPick).filter(DraftPick.draft_id == draft.id, DraftPick.picked_at.isnot(None)).order_by(DraftPick.pick_number.desc()).first()
        if last_pick:
            completed_at = last_pick.picked_at
    series_start = calculate_nhl_series_start(db, completed_at, today)
    series_schedule.generate_schedule(db, league, series_start, 1, replace=True, force=force)
    try:
        db.commit()
    except Exception:
        pass
    return None


def ensure_schedule(db: Session, league: League, today: datetime.date | None = None) -> str | None:
    """Creates the league's schedule if it has none. Returns None if the league now has one, else why it can't."""
    if is_college(db, league.id):
        return "College leagues arrange their own schedule in the schedule stage"
    if today is None:
        try:
            from fantasy_sports.platform.nhl_daily_sync import current_eastern_date
            today = current_eastern_date()
        except Exception:
            today = datetime.date.today()

    from fantasy_sports.platform.db.models.core import Draft, DraftPick
    draft = db.query(Draft).filter_by(league_id=league.id).order_by(Draft.id.desc()).first()
    completed_at = draft.completed_at if draft else None
    if draft and not completed_at:
        last_pick = db.query(DraftPick).filter(DraftPick.draft_id == draft.id, DraftPick.picked_at.isnot(None)).order_by(DraftPick.pick_number.desc()).first()
        if last_pick:
            completed_at = last_pick.picked_at
    target_start = calculate_nhl_series_start(db, completed_at, today)

    if has_schedule(db, league):
        if series_schedule.league_is_nhl(db, league.id):
            from fantasy_sports.platform.db.models.core import LeagueSeries
            first_s = db.query(LeagueSeries).filter_by(league_id=league.id).order_by(LeagueSeries.start_date).first()
            if first_s and first_s.start_date != target_start and not series_schedule._started(db, league.id):
                try:
                    with db.begin_nested():
                        series_schedule.generate_schedule(db, league, target_start, 1, replace=True, force=True)
                    db.commit()
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning("ensure_schedule failed to regenerate schedule: %s", e)
        return None
    try:
        with db.begin_nested():            # a failed attempt leaves nothing behind
            if series_schedule.league_is_nhl(db, league.id):
                if getattr(league, "league_mode", "standard") == "weekly_redraft":
                    raise ValueError("This league plays weekly redraft contests, not series")
                series_schedule.generate_schedule(db, league, target_start, 1)
            else:
                schedule.create_schedule(db, league.id)
        db.commit()
    except ValueError as e:
        return str(e)
    return None

