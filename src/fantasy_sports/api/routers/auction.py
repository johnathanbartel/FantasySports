from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from fantasy_sports.api.auth import assert_commissioner, assert_owns_team, current_user, optional_user
from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
from fantasy_sports.api.schemas import (
    AuctionBidOut,
    AuctionBudgetOut,
    AuctionDraftOut,
    AuctionRoundOut,
    AuctionStartRequest,
    DraftBoardAsset,
    NominateRequest,
    PlaceBidRequest,
)
from fantasy_sports.platform.auction import engine
from fantasy_sports.platform.ai import auction_ai
from fantasy_sports.platform.db.models.core import (
    AuctionBid, AuctionDraft, AuctionRound, AuctionTeamBudget, Team, User,
)
from fantasy_sports.platform.db.session import get_db

router = APIRouter(prefix="/auctions", tags=["auction"])


def _round_out(round_: AuctionRound) -> AuctionRoundOut:
    return AuctionRoundOut(
        id=round_.id,
        round_number=round_.round_number,
        draftable_asset_id=round_.draftable_asset_id,
        display_name=draftable_asset_display_name(round_.draftable_asset),
        nominated_by_team_id=round_.nominated_by_team_id,
        opens_at=round_.opens_at,
        closes_at=round_.closes_at,
        seconds_remaining=engine.seconds_remaining(round_),
        status=round_.status,
        winning_team_id=round_.winning_team_id,
        winning_bid_amount=round_.winning_bid_amount,
        was_tie_broken_randomly=round_.was_tie_broken_randomly,
    )


@router.post("/start", response_model=AuctionDraftOut)
def start_auction(
    payload: AuctionStartRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    assert_commissioner(db, user, payload.league_id)
    try:
        auction = engine.start_auction(db, payload.league_id, payload.round_seconds)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    db.refresh(auction)
    return auction


@router.get("/{auction_id}", response_model=AuctionDraftOut)
def get_auction(auction_id: int, db: Session = Depends(get_db)):
    auction = db.get(AuctionDraft, auction_id)
    if auction is None:
        raise HTTPException(404, "Auction draft not found")
    return auction


@router.get("/{auction_id}/budgets", response_model=list[AuctionBudgetOut])
def get_budgets(auction_id: int, db: Session = Depends(get_db)):
    budgets = db.query(AuctionTeamBudget).filter(AuctionTeamBudget.auction_draft_id == auction_id).all()
    return [
        AuctionBudgetOut(
            team_id=b.team_id,
            starting_budget=b.starting_budget,
            remaining_budget=b.remaining_budget,
            assets_won=b.assets_won,
        )
        for b in budgets
    ]


@router.get("/{auction_id}/board", response_model=list[DraftBoardAsset])
def get_board(auction_id: int, db: Session = Depends(get_db)):
    """Undrafted assets in the auction's league universe(s). Same pool for
    players and defense groups - no reserved rounds/budget bucket for
    defense (see the DL/LB/DB same-pool decision in docs/data_contract.md);
    a defense group can be nominated and bid on like any player."""
    auction = db.get(AuctionDraft, auction_id)
    if auction is None:
        raise HTTPException(404, "Auction draft not found")
    assets = engine.available_assets(db, auction)
    return [
        DraftBoardAsset(
            draftable_asset_id=a.id,
            asset_type=a.asset_type,
            display_name=draftable_asset_display_name(a),
            position=draftable_asset_position(a),
        )
        for a in assets
    ]


@router.post("/{auction_id}/nominate", response_model=AuctionRoundOut)
def nominate(
    auction_id: int, payload: NominateRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Opens a new timed sealed-bid round for the given asset. The caller
    must own the nominating team (and that team must be the one on the
    clock - the engine checks the latter)."""
    assert_owns_team(db, user, payload.nominated_by_team_id)
    try:
        round_ = engine.open_round(db, auction_id, payload.draftable_asset_id, payload.nominated_by_team_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    db.refresh(round_)
    return _round_out(round_)


@router.get("/{auction_id}/rounds/current", response_model=AuctionRoundOut | None)
def get_current_round(auction_id: int, db: Session = Depends(get_db)):
    """The open round, if any - lets a UI poll for "is there something to
    bid on right now" without tracking round ids itself. Registered ahead
    of the /{round_id} route below so "current" is never parsed as an id."""
    round_ = (
        db.query(AuctionRound)
        .filter(AuctionRound.auction_draft_id == auction_id, AuctionRound.status == "open")
        .order_by(AuctionRound.round_number.desc())
        .first()
    )
    return _round_out(round_) if round_ is not None else None


@router.get("/{auction_id}/rounds/{round_id}", response_model=AuctionRoundOut)
def get_round(auction_id: int, round_id: int, db: Session = Depends(get_db)):
    round_ = db.get(AuctionRound, round_id)
    if round_ is None or round_.auction_draft_id != auction_id:
        raise HTTPException(404, "Auction round not found")
    return _round_out(round_)


@router.post("/{auction_id}/rounds/{round_id}/bids", response_model=AuctionBidOut)
def place_bid(
    auction_id: int, round_id: int, payload: PlaceBidRequest,
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    """Sealed bid - one per team per round, overwritable while the round
    stays open. The caller must own the bidding team."""
    assert_owns_team(db, user, payload.team_id)
    round_ = db.get(AuctionRound, round_id)
    if round_ is None or round_.auction_draft_id != auction_id:
        raise HTTPException(404, "Auction round not found")
    try:
        bid = engine.place_bid(db, round_id, payload.team_id, payload.amount)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return AuctionBidOut(team_id=bid.team_id, amount=bid.amount, submitted_at=bid.submitted_at)


@router.get("/{auction_id}/rounds/{round_id}/bids", response_model=list[AuctionBidOut])
def get_round_bids(
    auction_id: int, round_id: int, db: Session = Depends(get_db), user: User | None = Depends(optional_user)
):
    """Bids stay sealed while the round is open (a signed-in caller sees
    only their own teams' bids), and are all revealed together once it
    resolves."""
    round_ = db.get(AuctionRound, round_id)
    if round_ is None or round_.auction_draft_id != auction_id:
        raise HTTPException(404, "Auction round not found")
    query = db.query(AuctionBid).filter(AuctionBid.auction_round_id == round_id)
    if round_.status == "open":
        if user is None:
            return []
        own_team_ids = [t.id for t in db.query(Team).filter(Team.owner_user_id == user.id)]
        query = query.filter(AuctionBid.team_id.in_(own_team_ids))
    bids = query.order_by(AuctionBid.amount.desc()).all()
    return [AuctionBidOut(team_id=b.team_id, amount=b.amount, submitted_at=b.submitted_at) for b in bids]


@router.post("/{auction_id}/rounds/{round_id}/resolve", response_model=AuctionRoundOut)
def resolve_round(
    auction_id: int, round_id: int, force: bool = False,
    db: Session = Depends(get_db), authorization: str | None = Header(default=None),
):
    """Reveals bids, picks the winner (ties broken by random choice among
    the tied teams), debits their budget, and advances nomination order.
    force=True is a commissioner override to resolve before the window
    closes - normal callers should let the timer run out."""
    round_ = db.get(AuctionRound, round_id)
    if round_ is None or round_.auction_draft_id != auction_id:
        raise HTTPException(404, "Auction round not found")
    if force:
        # Only the override needs identity: resolving a round whose window
        # has closed is harmless for anyone to trigger.
        user = current_user(authorization, db)
        assert_commissioner(db, user, db.get(AuctionDraft, auction_id).league_id)
    try:
        round_ = engine.resolve_round(db, round_id, force=force)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    db.refresh(round_)
    return _round_out(round_)


@router.post("/{auction_id}/ai/tick")
def ai_tick(auction_id: int, db: Session = Depends(get_db)):
    """Advances every AI-owned team's side of the auction: resolves an
    expired round, has AI teams place their sealed bids on an open round,
    or - when an AI team is on the clock - nominates for it. Takes no team
    argument and never acts for a human-owned team, so there is nothing to
    spoof; a caller can only make the AI move sooner. The board polls this
    on a short interval (a background worker would make the same call)."""
    if db.get(AuctionDraft, auction_id) is None:
        raise HTTPException(404, "Auction draft not found")
    actions = auction_ai.tick(db, auction_id)
    db.commit()
    return {"actions": actions}
