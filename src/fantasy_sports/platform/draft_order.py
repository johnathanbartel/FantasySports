"""Manual draft order.

leagues.draft_order_mode is 'random' (the default: the draft screen shuffles the order when the draft starts, as before) or
'manual' (the commissioner fixes who picks first; teams.draft_position holds each team's 1-based slot). In manual mode the
first-round order is the saved one, whatever the draft screen sends. Later rounds follow from it (snake reverses every other
round). The order is locked once a draft has started, because the picks already exist.
"""
from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import Draft, League, Team

MODES = ("random", "manual")


class DraftOrderError(ValueError):
    pass


def league_teams(db: Session, league_id: int) -> list[Team]:
    return db.query(Team).filter_by(league_id=league_id).order_by(Team.id).all()


def manual_order(db: Session, league: League) -> list[Team] | None:
    """The league's teams in their saved draft order, or None when the league drafts in random order. A team without a
    position (added after the order was saved) goes last, by id."""
    if league.draft_order_mode != "manual":
        return None
    teams = league_teams(db, league.id)
    return sorted(teams, key=lambda t: (t.draft_position is None, t.draft_position or 0, t.id))


def validate_positions(positions: list[int], size: int) -> None:
    if sorted(positions) != list(range(1, size + 1)):
        raise DraftOrderError(f"Draft positions must use each number from 1 to {size} exactly once")


def set_order(db: Session, league: League, mode: str, team_order: list[int] | None) -> None:
    """Saves the mode and, for 'manual', the order (team ids, first pick first; every team exactly once)."""
    if mode not in MODES:
        raise DraftOrderError(f"mode must be one of {MODES}")
    if db.query(Draft.id).filter(Draft.league_id == league.id, Draft.status != "pending").first() is not None:
        raise DraftOrderError("The draft has already started, so its order can no longer be changed")
    teams = league_teams(db, league.id)
    if mode == "random":
        for t in teams:
            t.draft_position = None
        league.draft_order_mode = "random"
        return
    if team_order is None or sorted(team_order) != sorted(t.id for t in teams):
        raise DraftOrderError("The draft order must list every team in the league exactly once")
    by_id = {t.id: t for t in teams}
    for position, team_id in enumerate(team_order, start=1):
        by_id[team_id].draft_position = position
    league.draft_order_mode = "manual"
