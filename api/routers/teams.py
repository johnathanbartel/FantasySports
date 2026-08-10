from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.helpers import draftable_asset_display_name, draftable_asset_position
from api.schemas import DraftBoardAsset, RosterEntryOut, TeamOut
from db.models.core import DraftableAsset, League, LeagueUniverse, RosterEntry, Team
from db.session import get_db

router = APIRouter(prefix="/teams", tags=["teams"])


@router.get("/{team_id}", response_model=TeamOut)
def get_team(team_id: int, db: Session = Depends(get_db)):
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found")
    return team


@router.get("/{team_id}/roster", response_model=list[RosterEntryOut])
def get_roster(team_id: int, db: Session = Depends(get_db)):
    entries = db.query(RosterEntry).filter(RosterEntry.team_id == team_id).all()
    return [
        RosterEntryOut(
            draftable_asset_id=e.draftable_asset_id,
            asset_type=e.draftable_asset.asset_type,
            display_name=draftable_asset_display_name(e.draftable_asset),
        )
        for e in entries
    ]


@router.get("/{team_id}/free-agents", response_model=list[DraftBoardAsset])
def get_free_agents(team_id: int, db: Session = Depends(get_db)):
    """Draftable assets in the team's league universe(s) not yet on any
    roster in that league."""
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found")

    universe_ids = [
        lu.universe_id
        for lu in db.query(LeagueUniverse).filter(LeagueUniverse.league_id == team.league_id)
    ]
    league_team_ids = [t.id for t in db.query(Team).filter(Team.league_id == team.league_id)]
    rostered_asset_ids = {
        r.draftable_asset_id
        for r in db.query(RosterEntry).filter(RosterEntry.team_id.in_(league_team_ids))
    }

    candidates = (
        db.query(DraftableAsset).filter(DraftableAsset.universe_id.in_(universe_ids)).all()
    )
    return [
        DraftBoardAsset(
            draftable_asset_id=a.id,
            asset_type=a.asset_type,
            display_name=draftable_asset_display_name(a),
            position=draftable_asset_position(a),
        )
        for a in candidates
        if a.id not in rostered_asset_ids
    ]
