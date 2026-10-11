"""Features that depend on the kind of league. Only college leagues (College Football, College Basketball) have a
rankings poll and let teams set their own schedules; pro leagues (NFL, NHL...) do neither, for now."""

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import LeagueUniverse, Universe


def is_college(db: Session, league_id: int) -> bool:
    return db.query(LeagueUniverse).join(Universe, Universe.id == LeagueUniverse.universe_id).filter(
        LeagueUniverse.league_id == league_id, Universe.level == "college").count() > 0
