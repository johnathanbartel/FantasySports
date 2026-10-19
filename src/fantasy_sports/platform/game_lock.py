"""
The game-start lock: a player is locked the moment his own real game starts. A locked player can't be signed,
dropped, claimed off waivers, traded, or moved in a lineup, until his period ends. Every sport uses the same rule;
only the period differs:

  * hockey: the fantasy window (Sun-Tue, Wed-Thu, Fri-Sat). A player with several games in the window locks at
    the FIRST of them, whichever game ends up counting for the window;
  * every other sport: the week.

A player whose team has no game in the period (a bye) is never locked in it. A defense group locks with its team.
Once a period is over everybody in it is locked for lineup purposes (only a commissioner-mode commissioner can edit
history), but roster moves reopen, since nobody is playing.

Kickoffs are stored in more than one way, so they are normalised to naive UTC first (see kickoff_utc).

Known limit: NFL kickoffs are stored as a date with no time of day, so an NFL player locks at midnight Eastern on
his game day, not at kickoff.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import (
    DefenseGroup, DraftableAsset, Game, LeagueUniverse, LineupEntry, Player, Universe, Week,
)
from fantasy_sports.platform.window_sports import is_window_sport
from fantasy_sports.sports.hockey import pipeline as hockey_pipeline

# Window sports (NHL, NBA) are locked per fantasy window, the rest per week; see platform/window_sports.py.


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
    if is_window_sport(universe_code):
        return eastern_to_utc(k) if k.hour >= 5 else k
    if k.time() == datetime.time(0):
        return eastern_to_utc(k)
    return k


def week_end(week: Week) -> datetime.date | None:
    return week.end_date or (week.start_date + datetime.timedelta(days=6) if week.start_date else None)


def week_over(week: Week, now: datetime.datetime) -> bool:
    """The week's last day has ended (Eastern)."""
    end = week_end(week)
    return end is not None and utc_to_eastern(now).date() > end


def _window_dates(today: datetime.date) -> list[datetime.date]:
    start, index = hockey_pipeline.window_of(today)
    return hockey_pipeline.window_dates(start, index)


def _started_teams(db: Session, universe_id: int, code: str | None, now: datetime.datetime, *,
                   week_id: int | None = None, dates: list[datetime.date] | None = None) -> set[int]:
    """Real team ids with a game in the period (a week, or a run of calendar dates) that has already kicked off."""
    query = db.query(Game).filter(Game.universe_id == universe_id)
    if week_id is not None:
        query = query.filter(Game.week_id == week_id)
    else:
        lo = datetime.datetime.combine(dates[0], datetime.time.min) - datetime.timedelta(days=1)
        hi = datetime.datetime.combine(dates[-1], datetime.time.min) + datetime.timedelta(days=2)
        query = query.filter(Game.kickoff_at >= lo, Game.kickoff_at < hi)
    teams: set[int] = set()
    for g in query:
        k = kickoff_utc(g, code)
        if k is None or k > now:
            continue
        if dates is not None and utc_to_eastern(k).date() not in dates:
            continue
        teams.update((g.home_team_id, g.away_team_id))
    return teams


def _assets_of_teams(db: Session, universe_id: int, team_ids: set[int]) -> set[int]:
    if not team_ids:
        return set()
    base = db.query(DraftableAsset.id).filter(DraftableAsset.universe_id == universe_id)
    players = base.join(Player, Player.id == DraftableAsset.player_id).filter(Player.real_team_id.in_(team_ids))
    groups = base.join(DefenseGroup, DefenseGroup.id == DraftableAsset.defense_group_id).filter(
        DefenseGroup.real_team_id.in_(team_ids))
    return {r[0] for r in players} | {r[0] for r in groups}


def locked_assets(db: Session, league_id: int, now: datetime.datetime | None = None) -> set[int]:
    """Every asset (player or defense group) in the league's universes whose game in the current period has started."""
    now = now or datetime.datetime.utcnow()
    today = utc_to_eastern(now).date()
    out: set[int] = set()
    for lu in db.query(LeagueUniverse).filter_by(league_id=league_id):
        universe = db.get(Universe, lu.universe_id)
        code = universe.code if universe else None
        if is_window_sport(code):
            teams = _started_teams(db, lu.universe_id, code, now, dates=_window_dates(today))
            out |= _assets_of_teams(db, lu.universe_id, teams)
            continue
        teams: set[int] = set()
        for week in db.query(Week).filter_by(universe_id=lu.universe_id):
            end = week_end(week)
            if week.start_date is not None and end is not None and week.start_date <= today <= end:
                teams |= _started_teams(db, lu.universe_id, code, now, week_id=week.id)
        out |= _assets_of_teams(db, lu.universe_id, teams)
    return out


def locked_assets_for_week(db: Session, week: Week, now: datetime.datetime | None = None) -> set[int] | None:
    """Assets locked in a lineup for `week`: those whose game has started, or None for everybody (the week is over).
    A hockey week is judged by its current window."""
    now = now or datetime.datetime.utcnow()
    if week_over(week, now):
        return None
    universe = db.get(Universe, week.universe_id)
    code = universe.code if universe else None
    today = utc_to_eastern(now).date()
    if is_window_sport(code):
        end = week_end(week)
        if week.start_date is None or end is None or not (week.start_date <= today <= end):
            return set()
        teams = _started_teams(db, week.universe_id, code, now, dates=_window_dates(today))
    else:
        teams = _started_teams(db, week.universe_id, code, now, week_id=week.id)
    return _assets_of_teams(db, week.universe_id, teams)


def _name(db: Session, asset_id: int) -> str:
    from fantasy_sports.api.helpers import draftable_asset_display_name
    asset = db.get(DraftableAsset, asset_id)
    return draftable_asset_display_name(asset) if asset else f"Player {asset_id}"


def _period_word(db: Session, asset_id: int) -> str:
    asset = db.get(DraftableAsset, asset_id)
    universe = db.get(Universe, asset.universe_id) if asset else None
    return "window" if universe is not None and is_window_sport(universe.code) else "week"


def lock_message(db: Session, asset_id: int) -> str:
    return f"{_name(db, asset_id)}'s game has started - he is locked until the {_period_word(db, asset_id)} ends"


def assert_unlocked(db: Session, league_id: int, asset_ids, now: datetime.datetime | None = None) -> None:
    """Raises ValueError (a message meant for the user) if any of the assets is locked right now."""
    wanted = [a for a in asset_ids]
    if not wanted:
        return
    locked = locked_assets(db, league_id, now)
    for asset_id in wanted:
        if asset_id in locked:
            raise ValueError(lock_message(db, asset_id))


def lineup_problem(db: Session, team_id: int, week: Week, new: dict[int, int],
                   now: datetime.datetime | None = None) -> str | None:
    """Why the lineup `new` (asset id -> roster slot id) can't replace the saved one for `week`, or None. A locked
    asset must stay exactly where it is: same slot, or still out of the lineup."""
    locked = locked_assets_for_week(db, week, now)
    if locked is not None and not locked:
        return None
    old = {e.draftable_asset_id: e.roster_slot_id for e in db.query(LineupEntry).filter_by(team_id=team_id, week_id=week.id)}
    for asset_id in sorted(set(old) | set(new)):
        if (locked is None or asset_id in locked) and old.get(asset_id) != new.get(asset_id):
            return lock_message(db, asset_id) if locked is not None else "That week is over - lineups are locked"
    return None
