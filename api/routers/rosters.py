from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.helpers import draftable_asset_display_name
from api.schemas import LineupEntryOut, LineupSetRequest
from db.models.core import LeagueUniverse, LineupEntry, RosterSlotDefinition, Team
from db.session import get_db

router = APIRouter(prefix="/teams", tags=["rosters"])


def _primary_universe_id(db: Session, team: Team) -> int:
    """A single-universe league has exactly one row here. Dual Universe
    leagues have two; which one governs roster slots is exactly the kind
    of crossover mechanic the spec flags as TBD, so this takes the first
    one as a placeholder rather than guessing at real behavior."""
    lu = db.query(LeagueUniverse).filter(LeagueUniverse.league_id == team.league_id).first()
    if lu is None:
        raise HTTPException(400, "League has no universe assigned")
    return lu.universe_id


@router.put("/{team_id}/lineup", response_model=list[LineupEntryOut])
def set_lineup(team_id: int, payload: LineupSetRequest, db: Session = Depends(get_db)):
    """Replaces the team's lineup for the given week with the submitted
    slot assignments.

    TODO (out of scope for this pass): validate that each assigned asset
    is actually on the team's roster, that its position is eligible for
    the slot per RosterSlotDefinition.eligible_positions, and that slot
    counts aren't exceeded. This endpoint currently trusts the caller.
    """
    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found")
    universe_id = _primary_universe_id(db, team)

    slot_by_code = {
        s.slot_code: s
        for s in db.query(RosterSlotDefinition).filter(
            RosterSlotDefinition.universe_id == universe_id
        )
    }

    db.query(LineupEntry).filter(
        LineupEntry.team_id == team_id, LineupEntry.week_id == payload.week_id
    ).delete()

    entries = []
    for slot_code, asset_id in payload.assignments.items():
        slot = slot_by_code.get(slot_code)
        if slot is None:
            raise HTTPException(400, f"Unknown roster slot '{slot_code}' for this universe")
        entry = LineupEntry(
            team_id=team_id,
            week_id=payload.week_id,
            draftable_asset_id=asset_id,
            roster_slot_id=slot.id,
        )
        db.add(entry)
        entries.append((entry, slot_code))

    db.commit()
    return [
        LineupEntryOut(
            week_id=payload.week_id,
            roster_slot_id=entry.roster_slot_id,
            slot_code=slot_code,
            draftable_asset_id=entry.draftable_asset_id,
            display_name=draftable_asset_display_name(entry.draftable_asset),
        )
        for entry, slot_code in entries
    ]


@router.get("/{team_id}/lineup", response_model=list[LineupEntryOut])
def get_lineup(team_id: int, week_id: int, db: Session = Depends(get_db)):
    entries = (
        db.query(LineupEntry)
        .filter(LineupEntry.team_id == team_id, LineupEntry.week_id == week_id)
        .all()
    )
    return [
        LineupEntryOut(
            week_id=week_id,
            roster_slot_id=e.roster_slot_id,
            slot_code=e.roster_slot.slot_code,
            draftable_asset_id=e.draftable_asset_id,
            display_name=draftable_asset_display_name(e.draftable_asset),
        )
        for e in entries
    ]
