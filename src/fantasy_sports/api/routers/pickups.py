"""Trending and recommended pickups (see platform/pickups.py for the rules).

  GET /leagues/{league_id}/trending-pickups      free agents other leagues are adding (every sport)
  GET /teams/{team_id}/recommended-pickups       free agents who would raise the team's projected lineup (NHL)
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from fantasy_sports.platform import draft_grades, pickups
from fantasy_sports.platform.db.models.core import League, Team
from fantasy_sports.platform.db.session import get_db

router = APIRouter(tags=["pickups"])


@router.get("/leagues/{league_id}/trending-pickups")
def trending_pickups(league_id: int, db: Session = Depends(get_db)):
    """This league's free agents ranked by the share of other leagues that added them in the last week. `available` is
    false, with a reason, when there is nothing to compare with."""
    if db.get(League, league_id) is None:
        raise HTTPException(404, "League not found")
    return pickups.trending(db, league_id)


@router.get("/teams/{team_id}/recommended-pickups")
def recommended_pickups(team_id: int, db: Session = Depends(get_db)):
    """The free agents who would add the most projected points to the team's lineup, on the engine behind pick suggestions."""
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found")
    if not draft_grades.is_nhl_league(db, team.league_id):
        raise HTTPException(400, "Recommended pickups are only available for NHL leagues")
    return pickups.recommended(db, team)
