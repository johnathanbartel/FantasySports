from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
from fantasy_sports.api.schemas import DraftBoardAsset, RosterEntryOut, TeamCreateRequest, TeamOut
from fantasy_sports.platform.conferences import service as conferences
from fantasy_sports.platform.db.models.core import DraftableAsset, League, LeagueUniverse, RosterEntry, Team
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.platform.team_tiers import PROGRAM_SIZES

router = APIRouter(prefix="/teams", tags=["teams"])


@router.post("", response_model=TeamOut)
def create_team(payload: TeamCreateRequest, db: Session = Depends(get_db)):
    """The team-creation screen this backs: a user picks a name and a
    program_size, and is placed into that tier's least-populated conference
    (see platform/conferences/service.py). This is the first real
    "create team" endpoint - every existing team in this codebase (seed
    scripts, smoke tests) was inserted directly via the ORM instead."""
    league = db.get(League, payload.league_id)
    if league is None:
        raise HTTPException(404, "League not found")
    if payload.program_size not in PROGRAM_SIZES:
        raise HTTPException(400, f"Invalid program_size '{payload.program_size}'")
    if payload.is_ai_owned == (payload.owner_user_id is not None):
        raise HTTPException(400, "Exactly one of owner_user_id or is_ai_owned must be set")

    try:
        conference = conferences.assign_team_to_conference(db, league.id, payload.program_size)
    except ValueError as e:
        raise HTTPException(400, str(e))

    team = Team(
        league_id=league.id,
        name=payload.name,
        owner_user_id=payload.owner_user_id,
        is_ai_owned=payload.is_ai_owned,
        program_size=payload.program_size,
        conference_id=conference.id,
    )
    db.add(team)
    db.commit()
    db.refresh(team)
    return team


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
