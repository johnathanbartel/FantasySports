from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_can_manage_team, assert_lineup_editable, current_user
from fantasy_sports.api.helpers import draftable_asset_display_name
from fantasy_sports.api.schemas import LineupEntryOut, LineupSetRequest
from fantasy_sports.platform import lineups
from fantasy_sports.platform.db.models.core import LeagueUniverse, LineupEntry, Team, User, Week
from fantasy_sports.platform.db.session import get_db

router = APIRouter(prefix="/teams", tags=["rosters"])


def _week_for_team(db: Session, team: Team, week_id: int) -> Week:
    week = db.get(Week, week_id)
    universe_ids = {lu.universe_id for lu in db.query(LeagueUniverse).filter_by(league_id=team.league_id)}
    if week is None or week.universe_id not in universe_ids:
        raise HTTPException(400, "That week isn't part of this league's universe")
    return week


def _out(entries: list[LineupEntry], week_id: int) -> list[LineupEntryOut]:
    return [
        LineupEntryOut(
            week_id=week_id, roster_slot_id=e.roster_slot_id, slot_code=e.roster_slot.slot_code,
            draftable_asset_id=e.draftable_asset_id, display_name=draftable_asset_display_name(e.draftable_asset),
        )
        for e in entries
    ]


@router.put("/{team_id}/lineup", response_model=list[LineupEntryOut])
def set_lineup(
    team_id: int, payload: LineupSetRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Replaces the team's lineup for the week. assignments maps a slot code to
    a player id, or - for a slot that holds several players, like RB or BENCH -
    a list of ids. Every player must be on the team's roster and eligible for
    the slot, and no slot may be over its count. The caller must own the team;
    for an AI-owned team, the league's commissioner may act for it, but only
    with commissioner mode (a premium feature)."""
    team = assert_can_manage_team(db, user, team_id)
    assert_lineup_editable(db, user, team, _week_for_team(db, team, payload.week_id))
    try:
        pairs = lineups.build_lineup(db, team, payload.assignments)
    except ValueError as e:
        raise HTTPException(400, str(e))
    entries = lineups.save_lineup(db, team, payload.week_id, pairs)
    db.commit()
    return _out(entries, payload.week_id)


@router.post("/{team_id}/lineup/auto", response_model=list[LineupEntryOut])
def auto_lineup(team_id: int, week_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Sets the team's best lineup for the week (the same logic the AI teams use)."""
    team = assert_can_manage_team(db, user, team_id)
    assert_lineup_editable(db, user, team, _week_for_team(db, team, week_id))
    pairs = lineups.build_lineup(db, team, lineups.best_lineup(db, team))
    entries = lineups.save_lineup(db, team, week_id, pairs)
    db.commit()
    return _out(entries, week_id)


@router.get("/{team_id}/lineup", response_model=list[LineupEntryOut])
def get_lineup(team_id: int, week_id: int, db: Session = Depends(get_db)):
    entries = (
        db.query(LineupEntry)
        .filter(LineupEntry.team_id == team_id, LineupEntry.week_id == week_id)
        .order_by(LineupEntry.id)
        .all()
    )
    return _out(entries, week_id)
