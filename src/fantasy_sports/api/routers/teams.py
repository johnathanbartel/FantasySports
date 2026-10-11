from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_commissioner, current_user
from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
from fantasy_sports.api.schemas import DraftBoardAsset, RosterEntryOut, TeamCreateRequest, TeamOut
from fantasy_sports.platform import lineups
from fantasy_sports.platform.conferences import service as conferences
from fantasy_sports.platform.draft_stats import stats_by_asset
from fantasy_sports.platform.roster_fill import fill_slots
from fantasy_sports.platform.db.models.core import (
    DraftableAsset, League, LeagueUniverse, RealTeam, RosterEntry, Team, User,
)
from fantasy_sports.platform.recruiting.pricing import season_points_by_asset
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.platform.defense_modes import league_pool
from fantasy_sports.platform.team_tiers import PROGRAM_SIZES

router = APIRouter(prefix="/teams", tags=["teams"])


@router.post("", response_model=TeamOut)
def create_team(
    payload: TeamCreateRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
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
    if payload.is_ai_owned:
        assert_commissioner(db, user, league.id)
    elif payload.owner_user_id != user.id:
        raise HTTPException(403, "You can only create a team you own yourself")
    if db.query(Team).filter(Team.league_id == league.id).count() >= league.size:
        raise HTTPException(400, f"League is full ({league.size} teams)")

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
    points = season_points_by_asset(db)
    school = {t.id: t.name for t in db.query(RealTeam)}
    out = []
    for e in entries:
        a = e.draftable_asset
        out.append(RosterEntryOut(
            draftable_asset_id=e.draftable_asset_id,
            asset_type=a.asset_type,
            display_name=draftable_asset_display_name(a),
            position=draftable_asset_position(a),
            real_team=school.get(a.player.real_team_id if a.player else a.defense_group.real_team_id),
            last_season_points=points.get(a.id),
            acquired_via=e.acquired_via,
        ))
    return out


class LinePlayer(BaseModel):
    draftable_asset_id: int
    display_name: str
    position: str | None
    real_team: str | None
    points: float | None   # last season's points (None = no stored data)


class LineSlot(BaseModel):
    slot_code: str
    eligible_positions: list[str]
    is_starter: bool
    capacity: int
    players: list[LinePlayer]   # fewer than capacity = open spots


class LinesOut(BaseModel):
    team_id: int
    team_name: str
    starters_filled: int
    starters_total: int
    slots: list[LineSlot]
    unslotted: list[LinePlayer]   # drafted beyond every slot


@router.get("/{team_id}/lines", response_model=LinesOut)
def get_lines(team_id: int, db: Session = Depends(get_db)):
    """The team's roster laid out by lineup slot (C, W, D, G, bench... / QB, RB, ...), with open slots shown, so a
    manager can watch the lines fill as the draft goes."""
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found")
    try:
        slots = lineups.league_slots(db, team.league_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    entries = db.query(RosterEntry).filter(RosterEntry.team_id == team_id).order_by(RosterEntry.id).all()
    stats = stats_by_asset(db, [e.draftable_asset_id for e in entries])

    def player(e: RosterEntry) -> LinePlayer:
        a = e.draftable_asset
        st = stats.get(a.id, {})
        return LinePlayer(draftable_asset_id=a.id, display_name=draftable_asset_display_name(a),
                          position=draftable_asset_position(a), real_team=st.get("team"), points=st.get("points"))

    by_asset = {e.draftable_asset_id: player(e) for e in entries}
    filled, overflow = fill_slots(slots, [(e.draftable_asset_id, by_asset[e.draftable_asset_id].position) for e in entries])
    filled.sort(key=lambda sc: (not sc[0].is_starter, sc[0].id))   # show in the universe's own order (C, W, D, G, bench)
    out = [LineSlot(slot_code=s.slot_code, eligible_positions=list(s.eligible_positions), is_starter=s.is_starter,
                    capacity=s.slot_count, players=[by_asset[i] for i, _ in chosen]) for s, chosen in filled]
    return LinesOut(
        team_id=team.id, team_name=team.name,
        starters_filled=sum(len(s.players) for s in out if s.is_starter),
        starters_total=sum(s.capacity for s in out if s.is_starter),
        slots=out, unslotted=[by_asset[i] for i, _ in overflow])


@router.get("/{team_id}/free-agents", response_model=list[DraftBoardAsset])
def get_free_agents(team_id: int, db: Session = Depends(get_db)):
    """Draftable assets in the team's league universe(s) not yet on any
    roster in that league."""
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found")

    league_team_ids = [t.id for t in db.query(Team).filter(Team.league_id == team.league_id)]
    rostered_asset_ids = {
        r.draftable_asset_id
        for r in db.query(RosterEntry).filter(RosterEntry.team_id.in_(league_team_ids))
    }

    candidates = league_pool(db, team.league_id)
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
