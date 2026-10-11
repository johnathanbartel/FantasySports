"""Features that depend on the kind of league. Only college leagues (College Football, College Basketball) have a
rankings poll and let teams set their own schedules; pro leagues (NFL, NHL...) do neither, for now."""

from sqlalchemy.orm import Session

from fantasy_sports.platform.db.models.core import LeagueUniverse, Universe


def is_college(db: Session, league_id: int) -> bool:
    return db.query(LeagueUniverse).join(Universe, Universe.id == LeagueUniverse.universe_id).filter(
        LeagueUniverse.league_id == league_id, Universe.level == "college").count() > 0


def draft_state(db: Session, league_id: int) -> str:
    """not_started / in_progress / complete across snake, auction and recruiting drafts."""
    from fantasy_sports.platform.db.models.core import AuctionDraft, Draft, RecruitingDraft
    statuses = [d.status for d in db.query(Draft).filter(Draft.league_id == league_id)]
    statuses += [d.status for d in db.query(AuctionDraft).filter(AuctionDraft.league_id == league_id)]
    statuses += [d.status for d in db.query(RecruitingDraft).filter(RecruitingDraft.league_id == league_id)]
    if not statuses:
        return "not_started"
    return "complete" if all(s in ("complete", "closed") for s in statuses) else "in_progress"


def roster_spots(db: Session, league_id: int) -> int:
    """How many players a team's roster holds: every lineup slot (starters and bench). 0 if the league has no slots."""
    from fantasy_sports.platform import lineups
    try:
        return sum(s.slot_count for s in lineups.league_slots(db, league_id))
    except ValueError:
        return 0
