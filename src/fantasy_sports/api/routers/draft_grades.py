"""NHL draft grades and the projected-points power rankings (see platform/draft_grades.py for the rules)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import optional_user
from fantasy_sports.platform import draft_grades, entitlements
from fantasy_sports.platform.db.models.core import Team, User
from fantasy_sports.platform.db.session import get_db

router = APIRouter(tags=["draft-grades"])


def _require_nhl_league(db: Session, league_id: int) -> None:
    if not db.query(Team).filter_by(league_id=league_id).count():
        raise HTTPException(404, "League not found or has no teams")
    if not draft_grades.is_nhl_league(db, league_id):
        raise HTTPException(400, "Draft grades are only available for NHL leagues")


@router.get("/leagues/{league_id}/draft-grades")
def league_draft_grades(league_id: int, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    """Each team's grade for the newest completed snake draft, and every pick's value. `available` is false, with a reason,
    until a draft is complete and projections exist. A Premium feature (platform/entitlements.py): 402 otherwise."""
    _require_nhl_league(db, league_id)
    if not entitlements.has_feature(db, user, entitlements.DRAFT_GRADES):
        raise HTTPException(402, "Draft grades are a Premium feature. See /static/pricing.html to upgrade.")
    return draft_grades.draft_grades(db, league_id)


@router.get("/leagues/{league_id}/power-rankings")
def league_power_rankings(league_id: int, db: Session = Depends(get_db)):
    """Teams ranked by the projected points of the best lineup their roster can field."""
    _require_nhl_league(db, league_id)
    return draft_grades.power_rankings(db, league_id)
