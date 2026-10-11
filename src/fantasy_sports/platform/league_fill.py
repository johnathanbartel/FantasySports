"""A league can only draft once every one of its `size` team spots is taken (by a manager or an AI team)."""

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import League, Team


def assert_league_full(db: Session, league: League) -> None:
    """Raises ValueError if the league has fewer teams than its size. Applies to every draft type."""
    have = db.query(Team).filter(Team.league_id == league.id).count()
    if have < league.size:
        open_spots = league.size - have
        raise ValueError(
            f"This league isn't full yet: {have} of {league.size} teams. "
            f"Fill the {open_spots} open spot{'' if open_spots == 1 else 's'} (the commissioner can add AI teams) "
            "before starting the draft."
        )
