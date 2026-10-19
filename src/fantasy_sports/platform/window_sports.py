"""Sports that play the fantasy window / series calendar (a game per window, daily series between two teams).

Hockey was first; basketball plays the same calendar.  Everything that only asks "does this league use windows
and series?" reads it from here instead of hard-coding 'nhl'.  Anything that is genuinely NHL data (the NHL feed,
pecking-order lines, off-day banking, the hockey simulator) stays keyed to 'nhl'.
"""

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import LeagueUniverse, Universe

WINDOW_SPORTS = frozenset({"nhl", "nba"})


def is_window_sport(universe_code: str | None) -> bool:
    return universe_code in WINDOW_SPORTS


def window_universe_code(db: Session, league_id: int) -> str | None:
    """The league's window-sport universe code ('nhl' / 'nba'), or None for a league on the weekly calendar."""
    row = db.query(Universe.code).join(LeagueUniverse, LeagueUniverse.universe_id == Universe.id).filter(
        LeagueUniverse.league_id == league_id, Universe.code.in_(WINDOW_SPORTS)).first()
    return row[0] if row else None


def league_uses_windows(db: Session, league_id: int) -> bool:
    return window_universe_code(db, league_id) is not None
