"""
HTTP surface for the Timed Auction Draft, shaped so the existing draft board
frontend can drive a whole Recruiting Period:

    POST /auctions/start                      open a Recruiting Period
    GET  /auctions/{id}                       session + current round + clock
    GET  /auctions/{id}/board                 the revealed player pool
    GET  /auctions/{id}/budgets               each team's tier and money left
    POST /auctions/{id}/bids                  submit one blind bid
    GET  /auctions/{id}/rounds/{n}            blind while open, full reveal after
    POST /auctions/{id}/rounds/{n}/resolve    reveal -> debit -> advance
    GET  /auctions/{id}/results               everything won so far

The board reuses the snake draft's board query rather than reimplementing
"what's left", so both draft styles agree on the pool by construction.
"""

import datetime
import random

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from fantasy_sports.api.helpers import draftable_asset_display_name, draftable_asset_position
from fantasy_sports.api.routers.draft import get_draft_board
from fantasy_sports.api.schemas import (
    AuctionBidOut,
    AuctionBidRequest,
    AuctionBudgetOut,
    AuctionContestOut,
    AuctionRoundOut,
    AuctionRoundRevealOut,
    AuctionSessionOut,
    AuctionStartRequest,
    AuctionTierOut,
    DraftBoardAsset,
)
from fantasy_sports.platform.auction.config import (
    MINIMUM_ROUND_SECONDS,
    AuctionConfig,
    TierConfig,
)
from fantasy_sports.platform.auction.engine import (
    AuctionError,
    create_session,
    ensure_started,
    resolve_round,
    submit_bid,
)
from fantasy_sports.platform.db.models.auction import (
    AuctionBid,
    AuctionBudget,
    AuctionRound,
    AuctionRoundResult,
    AuctionSession,
    AuctionTier,
)
from fantasy_sports.platform.db.models.core import (
    DraftableAsset,
    League,
    LeagueUniverse,
    Team,
    Universe,
)
from fantasy_sports.platform.db.session import get_db
from fantasy_sports.sports.football.college.auction import NCAAF_AUCTION_DEFAULTS

router = APIRouter(prefix="/auctions", tags=["auction"])

# Per-sport defaults. The engine itself has none - a new sport adds its config
# module and one line here, not a branch inside the auction logic.
DEFAULTS_BY_UNIVERSE: dict[str, AuctionConfig] = {
    "ncaaf": NCAAF_AUCTION_DEFAULTS,
}


def _fail(exc: AuctionError) -> HTTPException:
    return HTTPException(exc.status_code, str(exc))


def _session_or_404(db: Session, auction_id: int) -> AuctionSession:
    session = db.get(AuctionSession, auction_id)
    if session is None:
        raise HTTPException(404, "Auction session not found")
    return session


def _defaults_for_league(db: Session, league_id: int) -> AuctionConfig | None:
    """The sport defaults for a league's universe, or None if it has none yet.

    A Dual Universe league would have two universes; we take the first one
    with defaults configured. A sport that hasn't registered defaults can
    still run an auction - the request just has to supply its own tiers.
    """
    codes = [
        code
        for (code,) in db.query(Universe.code)
        .join(LeagueUniverse, LeagueUniverse.universe_id == Universe.id)
        .filter(LeagueUniverse.league_id == league_id)
        .order_by(Universe.id)
    ]
    for code in codes:
        if code in DEFAULTS_BY_UNIVERSE:
            return DEFAULTS_BY_UNIVERSE[code]
    return None


def _round_out(round_row: AuctionRound, now: datetime.datetime) -> AuctionRoundOut:
    seconds_remaining = None
    if round_row.status == "open":
        seconds_remaining = max(0, int((round_row.closes_at - now).total_seconds()))
    return AuctionRoundOut(
        round_number=round_row.round_number,
        status=round_row.status,
        opens_at=round_row.opens_at,
        closes_at=round_row.closes_at,
        resolved_at=round_row.resolved_at,
        seconds_remaining=seconds_remaining,
    )


def _session_out(db: Session, session: AuctionSession) -> AuctionSessionOut:
    now = datetime.datetime.utcnow()
    current = None
    if session.current_round_number is not None:
        row = (
            db.query(AuctionRound)
            .filter(
                AuctionRound.auction_session_id == session.id,
                AuctionRound.round_number == session.current_round_number,
            )
            .one_or_none()
        )
        if row is not None:
            current = _round_out(row, now)

    tiers = (
        db.query(AuctionTier)
        .filter(AuctionTier.auction_session_id == session.id)
        .order_by(AuctionTier.sort_order, AuctionTier.id)
        .all()
    )
    return AuctionSessionOut(
        id=session.id,
        league_id=session.league_id,
        draft_id=session.draft_id,
        status=session.status,
        total_rounds=session.total_rounds,
        round_seconds=session.round_seconds,
        minimum_round_seconds=session.minimum_round_seconds,
        recruiting_period_seconds=session.round_seconds * session.total_rounds,
        min_bid=session.min_bid,
        starts_at=session.starts_at,
        completed_at=session.completed_at,
        current_round=current,
        tiers=[
            AuctionTierOut(
                code=t.code, display_name=t.display_name, starting_budget=t.starting_budget
            )
            for t in tiers
        ],
    )


def _bid_out(db: Session, bid: AuctionBid, round_number: int) -> AuctionBidOut:
    asset = db.get(DraftableAsset, bid.draftable_asset_id)
    return AuctionBidOut(
        id=bid.id,
        round_number=round_number,
        team_id=bid.team_id,
        draftable_asset_id=bid.draftable_asset_id,
        display_name=draftable_asset_display_name(asset) if asset else "?",
        amount=bid.amount,
        status=bid.status,
        lost_reason=bid.lost_reason,
        submitted_at=bid.submitted_at,
    )


def _contest_out(
    db: Session, result: AuctionRoundResult, round_number: int
) -> AuctionContestOut:
    asset = db.get(DraftableAsset, result.draftable_asset_id)
    return AuctionContestOut(
        round_number=round_number,
        draftable_asset_id=result.draftable_asset_id,
        display_name=draftable_asset_display_name(asset) if asset else "?",
        position=draftable_asset_position(asset) if asset else None,
        winning_team_id=result.winning_team_id,
        winning_amount=result.winning_amount,
        bid_count=result.bid_count,
        tie_break_used=result.tie_break_used,
        tied_team_ids=result.tied_team_ids or [],
    )


def _config_from_payload(
    payload: AuctionStartRequest, league: League, defaults: AuctionConfig | None
) -> AuctionConfig:
    """Layer the request's overrides onto the sport defaults.

    Anything the caller leaves unset falls back to the league (slot count) or
    the sport's configured defaults, so for a sport with defaults registered a
    bare `{"league_id": 1}` is a valid request that still produces a fully
    parameterised auction. A sport with no defaults yet can run one too - it
    just has to send its own `tiers`.
    """
    if payload.tiers is not None:
        if not payload.tiers:
            raise HTTPException(400, "At least one budget tier is required")
        tiers = tuple(
            TierConfig(
                code=t.code,
                display_name=t.display_name,
                starting_budget=t.starting_budget,
                sort_order=t.sort_order,
            )
            for t in payload.tiers
        )
        default_tier_code = payload.default_tier_code or tiers[0].code
    elif defaults is not None:
        tiers = defaults.tiers
        default_tier_code = payload.default_tier_code or defaults.default_tier_code
    else:
        raise HTTPException(
            400,
            "No auction defaults are configured for this league's universe(s); "
            "send `tiers` (and optionally `default_tier_code`) in the request",
        )

    # An explicit per-round window and an explicit total Recruiting Period are
    # alternatives; whichever the caller supplied replaces the sport default.
    if payload.round_seconds is not None:
        round_seconds, period_seconds = payload.round_seconds, None
    elif payload.recruiting_period_seconds is not None:
        round_seconds, period_seconds = None, payload.recruiting_period_seconds
    elif defaults is not None:
        round_seconds = defaults.round_seconds
        period_seconds = defaults.recruiting_period_seconds
    else:
        round_seconds = period_seconds = None  # falls through to the floor

    return AuctionConfig(
        slots=payload.slots or league.draft_rounds,
        tiers=tiers,
        default_tier_code=default_tier_code,
        round_seconds=round_seconds,
        recruiting_period_seconds=period_seconds,
        minimum_round_seconds=(
            defaults.minimum_round_seconds if defaults else MINIMUM_ROUND_SECONDS
        ),
        min_bid=payload.min_bid or (defaults.min_bid if defaults else 1),
    )


@router.post("/start", response_model=AuctionSessionOut)
def start_auction(payload: AuctionStartRequest, db: Session = Depends(get_db)):
    """Open a Recruiting Period for a league.

    Every parameter is optional: unset ones fall back to the league's settings
    (slot count) and the sport's configured defaults (window, tiers, min bid).
    """
    league = db.get(League, payload.league_id)
    if league is None:
        raise HTTPException(404, "League not found")

    try:
        config = _config_from_payload(payload, league, _defaults_for_league(db, league.id))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        session = create_session(
            db,
            league_id=league.id,
            config=config,
            team_tier_codes=payload.team_tiers,
            starts_at=payload.starts_at,
        )
    except AuctionError as exc:
        db.rollback()
        raise _fail(exc) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc

    db.commit()
    db.refresh(session)
    return _session_out(db, session)


@router.get("/{auction_id}", response_model=AuctionSessionOut)
def get_auction(auction_id: int, db: Session = Depends(get_db)):
    """Session state, including the current round and how long is left on it.

    Note this read does one write: a Recruiting Period scheduled for a future
    `starts_at` flips to in_progress here once that time has passed, so a
    scheduled auction opens on its own without waiting for the first bid.
    Rounds are never *resolved* on a read - that is always an explicit POST.
    """
    session = _session_or_404(db, auction_id)
    ensure_started(db, session)
    db.commit()
    return _session_out(db, session)


@router.get("/{auction_id}/board", response_model=list[DraftBoardAsset])
def get_auction_board(auction_id: int, db: Session = Depends(get_db)):
    """The revealed player pool: everything in the league's universe(s) that
    hasn't been won yet. Defense groups appear as individual DL/LB/DB lines,
    one per real team per group - they are never bundled (see
    docs/data_contract.md)."""
    session = _session_or_404(db, auction_id)
    return get_draft_board(session.draft_id, db)


@router.get("/{auction_id}/budgets", response_model=list[AuctionBudgetOut])
def get_auction_budgets(auction_id: int, db: Session = Depends(get_db)):
    session = _session_or_404(db, auction_id)
    rows = (
        db.query(AuctionBudget, AuctionTier, Team)
        .join(AuctionTier, AuctionTier.id == AuctionBudget.auction_tier_id)
        .join(Team, Team.id == AuctionBudget.team_id)
        .filter(AuctionBudget.auction_session_id == session.id)
        .order_by(Team.id)
        .all()
    )
    return [
        AuctionBudgetOut(
            team_id=budget.team_id,
            team_name=team.name,
            tier_code=tier.code,
            tier_display_name=tier.display_name,
            starting_budget=budget.starting_budget,
            spent_budget=budget.spent_budget,
            remaining_budget=budget.remaining_budget,
        )
        for budget, tier, team in rows
    ]


@router.post("/{auction_id}/bids", response_model=AuctionBidOut)
def place_bid(auction_id: int, payload: AuctionBidRequest, db: Session = Depends(get_db)):
    """Submit (or replace) one blind bid for the open round.

    The response echoes only the caller's own bid. No endpoint exposes another
    owner's bid until the round resolves.
    """
    session = _session_or_404(db, auction_id)
    try:
        bid = submit_bid(
            db,
            session,
            team_id=payload.team_id,
            draftable_asset_id=payload.draftable_asset_id,
            amount=payload.amount,
        )
    except AuctionError as exc:
        db.rollback()
        raise _fail(exc) from exc

    round_number = session.current_round_number or 0
    out = _bid_out(db, bid, round_number)
    db.commit()
    return out


@router.get("/{auction_id}/rounds/{round_number}", response_model=AuctionRoundRevealOut)
def get_auction_round(
    auction_id: int,
    round_number: int,
    team_id: int | None = Query(
        None, description="Echo back only this team's own bid while the round is open"
    ),
    db: Session = Depends(get_db),
):
    """Blind while the round is open, full reveal once it has resolved."""
    session = _session_or_404(db, auction_id)
    round_row = (
        db.query(AuctionRound)
        .filter(
            AuctionRound.auction_session_id == session.id,
            AuctionRound.round_number == round_number,
        )
        .one_or_none()
    )
    if round_row is None:
        raise HTTPException(404, "Round not found")

    bids = (
        db.query(AuctionBid)
        .filter(AuctionBid.auction_round_id == round_row.id)
        .order_by(AuctionBid.team_id)
        .all()
    )

    if round_row.status == "resolved":
        visible = bids
    elif team_id is not None:
        visible = [b for b in bids if b.team_id == team_id]
    else:
        visible = []

    results = (
        db.query(AuctionRoundResult)
        .filter(AuctionRoundResult.auction_round_id == round_row.id)
        .order_by(AuctionRoundResult.winning_amount.desc(), AuctionRoundResult.id)
        .all()
    )

    # Only a resolved round has a "next" - reading round 1 back later should
    # not claim whatever round the session happens to be on now.
    next_round_number = None
    if round_row.status == "resolved" and round_row.round_number < session.total_rounds:
        next_round_number = round_row.round_number + 1

    return AuctionRoundRevealOut(
        round_number=round_row.round_number,
        status=round_row.status,
        opens_at=round_row.opens_at,
        closes_at=round_row.closes_at,
        resolved_at=round_row.resolved_at,
        bid_count=len(bids),
        bids=[_bid_out(db, b, round_row.round_number) for b in visible],
        contests=[_contest_out(db, r, round_row.round_number) for r in results],
        next_round_number=next_round_number,
        session_complete=session.status == "complete",
    )


@router.post("/{auction_id}/rounds/{round_number}/resolve", response_model=AuctionRoundRevealOut)
def resolve_auction_round(
    auction_id: int,
    round_number: int,
    force: bool = Query(
        False,
        description="Resolve before the window expires (commissioner: everyone has bid)",
    ),
    seed: int | None = Query(
        None, description="Seed the tie-break RNG; for reproducible demos and tests"
    ),
    db: Session = Depends(get_db),
):
    """Reveal every bid in the round, debit the winners, advance to the next
    per-slot round (or finish the Recruiting Period)."""
    session = _session_or_404(db, auction_id)
    rng = random.Random(seed) if seed is not None else random.Random()
    try:
        resolution = resolve_round(db, session, round_number=round_number, rng=rng, force=force)
    except AuctionError as exc:
        db.rollback()
        raise _fail(exc) from exc

    db.commit()
    out = get_auction_round(auction_id, round_number, team_id=None, db=db)
    out.next_round_number = resolution.next_round_number
    out.session_complete = resolution.session_complete
    return out


@router.get("/{auction_id}/results", response_model=list[AuctionContestOut])
def get_auction_results(auction_id: int, db: Session = Depends(get_db)):
    """Every asset won so far, newest round last."""
    session = _session_or_404(db, auction_id)
    rows = (
        db.query(AuctionRoundResult, AuctionRound.round_number)
        .join(AuctionRound, AuctionRound.id == AuctionRoundResult.auction_round_id)
        .filter(AuctionRound.auction_session_id == session.id)
        .order_by(AuctionRound.round_number, AuctionRoundResult.winning_amount.desc())
        .all()
    )
    return [_contest_out(db, result, round_number) for result, round_number in rows]
