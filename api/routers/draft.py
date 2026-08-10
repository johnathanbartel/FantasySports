import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.helpers import draftable_asset_display_name, draftable_asset_position
from api.schemas import (
    DraftBoardAsset,
    DraftOut,
    DraftPickOut,
    DraftStartRequest,
    MakePickRequest,
)
from db.models.core import (
    Draft,
    DraftableAsset,
    DraftPick,
    League,
    LeagueUniverse,
    RosterEntry,
    Team,
)
from db.session import get_db

router = APIRouter(prefix="/drafts", tags=["draft"])


@router.post("/start", response_model=DraftOut)
def start_draft(payload: DraftStartRequest, db: Session = Depends(get_db)):
    league = db.get(League, payload.league_id)
    if league is None:
        raise HTTPException(404, "League not found")

    teams = db.query(Team).filter(Team.league_id == league.id).order_by(Team.id).all()
    if not teams:
        raise HTTPException(400, "League has no teams yet")
    # TODO: real draft-order randomization/settings. v1 uses team.id order.

    draft = Draft(league_id=league.id, status="in_progress", started_at=datetime.datetime.utcnow())
    db.add(draft)
    db.flush()

    pick_number = 1
    for round_number in range(1, league.draft_rounds + 1):
        order = teams if round_number % 2 == 1 else list(reversed(teams))
        for team in order:
            db.add(
                DraftPick(
                    draft_id=draft.id,
                    round=round_number,
                    pick_number=pick_number,
                    team_id=team.id,
                )
            )
            pick_number += 1

    db.commit()
    db.refresh(draft)
    return draft


@router.get("/{draft_id}", response_model=DraftOut)
def get_draft(draft_id: int, db: Session = Depends(get_db)):
    draft = db.get(Draft, draft_id)
    if draft is None:
        raise HTTPException(404, "Draft not found")
    return draft


@router.get("/{draft_id}/picks", response_model=list[DraftPickOut])
def get_pick_history(draft_id: int, db: Session = Depends(get_db)):
    picks = (
        db.query(DraftPick)
        .filter(DraftPick.draft_id == draft_id)
        .order_by(DraftPick.pick_number)
        .all()
    )
    return [
        DraftPickOut(
            id=p.id,
            round=p.round,
            pick_number=p.pick_number,
            team_id=p.team_id,
            draftable_asset_id=p.draftable_asset_id,
            display_name=draftable_asset_display_name(p.draftable_asset) if p.draftable_asset else None,
            picked_at=p.picked_at,
            is_autopick=p.is_autopick,
        )
        for p in picks
    ]


@router.get("/{draft_id}/board", response_model=list[DraftBoardAsset])
def get_draft_board(draft_id: int, db: Session = Depends(get_db)):
    """Draftable assets in the draft's league universe(s) not yet picked in
    this draft."""
    draft = db.get(Draft, draft_id)
    if draft is None:
        raise HTTPException(404, "Draft not found")

    universe_ids = [
        lu.universe_id
        for lu in db.query(LeagueUniverse).filter(LeagueUniverse.league_id == draft.league_id)
    ]
    already_picked = {
        p.draftable_asset_id
        for p in db.query(DraftPick).filter(
            DraftPick.draft_id == draft_id, DraftPick.draftable_asset_id.isnot(None)
        )
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
        if a.id not in already_picked
    ]


def _next_open_pick(db: Session, draft_id: int) -> DraftPick | None:
    return (
        db.query(DraftPick)
        .filter(DraftPick.draft_id == draft_id, DraftPick.draftable_asset_id.is_(None))
        .order_by(DraftPick.pick_number)
        .first()
    )


@router.post("/{draft_id}/pick", response_model=DraftPickOut)
def make_pick(draft_id: int, payload: MakePickRequest, db: Session = Depends(get_db)):
    """Fills the next open pick slot with the given asset.

    TODO (out of scope for this pass): enforce that the request is coming
    from the team actually on the clock, and validate the pick isn't
    already rostered elsewhere in the league. This endpoint currently
    trusts the caller on both counts.
    """
    pick = _next_open_pick(db, draft_id)
    if pick is None:
        raise HTTPException(400, "Draft has no open picks left")

    asset = db.get(DraftableAsset, payload.draftable_asset_id)
    if asset is None:
        raise HTTPException(404, "Draftable asset not found")

    pick.draftable_asset_id = asset.id
    pick.picked_at = datetime.datetime.utcnow()
    db.add(RosterEntry(team_id=pick.team_id, draftable_asset_id=asset.id, acquired_via="draft"))
    db.commit()
    db.refresh(pick)

    return DraftPickOut(
        id=pick.id,
        round=pick.round,
        pick_number=pick.pick_number,
        team_id=pick.team_id,
        draftable_asset_id=pick.draftable_asset_id,
        display_name=draftable_asset_display_name(asset),
        picked_at=pick.picked_at,
        is_autopick=pick.is_autopick,
    )


@router.post("/{draft_id}/autopick", response_model=DraftPickOut)
def autopick(draft_id: int, db: Session = Depends(get_db)):
    """Fills the next open pick with the first available board asset.

    TODO: real AI personality-driven pick logic (Team.personality_profile
    isn't consulted at all yet) - this is a placeholder so AI-owned teams
    don't stall a draft, not the actual trait system.
    """
    pick = _next_open_pick(db, draft_id)
    if pick is None:
        raise HTTPException(400, "Draft has no open picks left")

    board = get_draft_board(draft_id, db)
    if not board:
        raise HTTPException(400, "No draftable assets left")

    pick.draftable_asset_id = board[0].draftable_asset_id
    pick.picked_at = datetime.datetime.utcnow()
    pick.is_autopick = True
    db.add(
        RosterEntry(team_id=pick.team_id, draftable_asset_id=pick.draftable_asset_id, acquired_via="draft")
    )
    db.commit()
    db.refresh(pick)

    asset = db.get(DraftableAsset, pick.draftable_asset_id)
    return DraftPickOut(
        id=pick.id,
        round=pick.round,
        pick_number=pick.pick_number,
        team_id=pick.team_id,
        draftable_asset_id=pick.draftable_asset_id,
        display_name=draftable_asset_display_name(asset),
        picked_at=pick.picked_at,
        is_autopick=pick.is_autopick,
    )
