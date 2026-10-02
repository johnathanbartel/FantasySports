"""
Timed sealed-bid auction draft engine: round window math, budget debits, and
round resolution (bid -> reveal -> debit -> next round). Pure functions over
a SQLAlchemy Session - no FastAPI/HTTP here, so this can be driven directly
by a test script the same way the API router drives it.

Round shape: a team nominates one draftable asset; every team gets one
hidden bid within a fixed time window (AuctionDraft.round_seconds); at
resolution the highest bid wins, ties are broken by random choice among the
tied teams, the winner's budget is debited, and nomination turn rotates to
the next team regardless of who won.

Domain-rule violations raise ValueError with a caller-facing message; the
API router is expected to catch that and turn it into an HTTPException, the
same pattern already used for validation in api/routers/leagues.py.
"""

from __future__ import annotations

import datetime
import random

from sqlalchemy.orm import Session

from fantasy_sports.platform.auction.constants import MINIMUM_BID, STARTING_BUDGET_BY_PROGRAM_SIZE
from fantasy_sports.platform.defense_modes import league_pool
from fantasy_sports.platform.db.models.core import (
    AuctionBid,
    AuctionDraft,
    AuctionRound,
    AuctionTeamBudget,
    DraftableAsset,
    League,
    LeagueUniverse,
    RosterEntry,
    Team,
)


def start_auction(db: Session, league_id: int, round_seconds: int) -> AuctionDraft:
    league = db.get(League, league_id)
    if league is None:
        raise ValueError("League not found")

    teams = db.query(Team).filter(Team.league_id == league_id).order_by(Team.id).all()
    if not teams:
        raise ValueError("League has no teams yet")

    auction = AuctionDraft(
        league_id=league_id,
        status="in_progress",
        round_seconds=round_seconds,
        nomination_order=[t.id for t in teams],
        next_nominator_index=0,
        started_at=datetime.datetime.utcnow(),
    )
    db.add(auction)
    db.flush()

    for team in teams:
        starting_budget = STARTING_BUDGET_BY_PROGRAM_SIZE[team.program_size]
        db.add(
            AuctionTeamBudget(
                auction_draft_id=auction.id,
                team_id=team.id,
                starting_budget=starting_budget,
                remaining_budget=starting_budget,
            )
        )
    db.flush()
    return auction


def available_assets(db: Session, auction: AuctionDraft) -> list[DraftableAsset]:
    """Draftable assets in the auction's league universe(s) not yet sold in
    this auction. Deliberately doesn't special-case asset_type - a defense
    group competes in the same pool as any player, same as the snake draft
    board (api/routers/draft.py::get_draft_board) and the same-pool decision
    in docs/data_contract.md."""
    sold_asset_ids = {
        r.draftable_asset_id
        for r in db.query(AuctionRound).filter(
            AuctionRound.auction_draft_id == auction.id, AuctionRound.status == "resolved"
        )
    }
    candidates = league_pool(db, auction.league_id)
    return [a for a in candidates if a.id not in sold_asset_ids]


def round_window(
    opens_at: datetime.datetime, round_seconds: int
) -> tuple[datetime.datetime, datetime.datetime]:
    return opens_at, opens_at + datetime.timedelta(seconds=round_seconds)


def seconds_remaining(round_: AuctionRound, now: datetime.datetime | None = None) -> float:
    now = now or datetime.datetime.utcnow()
    return max(0.0, (round_.closes_at - now).total_seconds())


def is_round_expired(round_: AuctionRound, now: datetime.datetime | None = None) -> bool:
    now = now or datetime.datetime.utcnow()
    return now >= round_.closes_at


def open_round(
    db: Session, auction_draft_id: int, draftable_asset_id: int, nominated_by_team_id: int
) -> AuctionRound:
    auction = db.get(AuctionDraft, auction_draft_id)
    if auction is None:
        raise ValueError("Auction draft not found")
    if auction.status != "in_progress":
        raise ValueError("Auction is not in progress")

    still_open = (
        db.query(AuctionRound)
        .filter(AuctionRound.auction_draft_id == auction.id, AuctionRound.status == "open")
        .first()
    )
    if still_open is not None:
        raise ValueError("A round is already open - resolve it before nominating again")

    available_ids = {a.id for a in available_assets(db, auction)}
    if draftable_asset_id not in available_ids:
        raise ValueError("That asset isn't available - already sold or outside this league's universe(s)")

    expected_nominator = auction.nomination_order[auction.next_nominator_index]
    if nominated_by_team_id != expected_nominator:
        raise ValueError(f"Team {expected_nominator} is on the clock to nominate, not {nominated_by_team_id}")

    round_number = (
        db.query(AuctionRound).filter(AuctionRound.auction_draft_id == auction.id).count() + 1
    )
    opens_at, closes_at = round_window(datetime.datetime.utcnow(), auction.round_seconds)

    round_ = AuctionRound(
        auction_draft_id=auction.id,
        round_number=round_number,
        draftable_asset_id=draftable_asset_id,
        nominated_by_team_id=nominated_by_team_id,
        opens_at=opens_at,
        closes_at=closes_at,
        status="open",
    )
    db.add(round_)
    db.flush()
    return round_


def place_bid(
    db: Session,
    auction_round_id: int,
    team_id: int,
    amount: int,
    now: datetime.datetime | None = None,
) -> AuctionBid:
    now = now or datetime.datetime.utcnow()
    round_ = db.get(AuctionRound, auction_round_id)
    if round_ is None:
        raise ValueError("Auction round not found")
    if round_.status != "open":
        raise ValueError("This round is no longer open for bidding")
    if is_round_expired(round_, now):
        raise ValueError("The bid window for this round has closed")
    if amount < MINIMUM_BID:
        raise ValueError(f"Bid must be at least {MINIMUM_BID}")

    budget = (
        db.query(AuctionTeamBudget)
        .filter(
            AuctionTeamBudget.auction_draft_id == round_.auction_draft_id,
            AuctionTeamBudget.team_id == team_id,
        )
        .first()
    )
    if budget is None:
        raise ValueError("Team is not part of this auction")
    league = db.get(League, db.get(AuctionDraft, round_.auction_draft_id).league_id)
    if budget.assets_won >= league.draft_rounds:
        raise ValueError("Roster is already full - this team can't bid again")
    # Every other open roster slot still needs at least MINIMUM_BID, so a bid
    # can't spend the money those slots need (else the roster could never fill).
    slots_after_this = league.draft_rounds - budget.assets_won - 1
    max_bid = budget.remaining_budget - slots_after_this * MINIMUM_BID
    if amount > max_bid:
        raise ValueError(
            f"Bid of {amount} exceeds your max of {max_bid} "
            f"(remaining budget {budget.remaining_budget}, keeping {MINIMUM_BID} for each of "
            f"{slots_after_this} other open roster slots)"
        )

    existing = (
        db.query(AuctionBid)
        .filter(AuctionBid.auction_round_id == auction_round_id, AuctionBid.team_id == team_id)
        .first()
    )
    if existing is not None:
        existing.amount = amount
        existing.submitted_at = now
        db.flush()
        return existing

    bid = AuctionBid(auction_round_id=auction_round_id, team_id=team_id, amount=amount, submitted_at=now)
    db.add(bid)
    db.flush()
    return bid


def _open_budgets(db: Session, auction: AuctionDraft, roster_size: int) -> list[AuctionTeamBudget]:
    """Budgets of teams that still have roster slots to fill."""
    budgets = db.query(AuctionTeamBudget).filter(AuctionTeamBudget.auction_draft_id == auction.id).all()
    return [b for b in budgets if b.assets_won < roster_size]


def _unsold_streak(db: Session, auction: AuctionDraft) -> int:
    """How many of the most recent rounds in a row ended unsold."""
    streak = 0
    recent = (
        db.query(AuctionRound)
        .filter(AuctionRound.auction_draft_id == auction.id, AuctionRound.status != "open")
        .order_by(AuctionRound.round_number.desc())
        .all()
    )
    for r in recent:
        if r.status != "unsold":
            break
        streak += 1
    return streak


def _is_auction_complete(db: Session, auction: AuctionDraft) -> bool:
    """Complete when every roster is full, nobody can afford a minimum bid,
    the pool is empty, or the auction has stalled - a full nomination cycle
    (every team that still has an open slot got a turn to nominate) where
    nothing sold. Without the stall rule an unfillable auction would sit
    in_progress forever.

    Partial rosters are allowed by design: a team is never required to fill
    its roster, and may finish with as few recruits as it likes."""
    league = db.get(League, auction.league_id)
    open_budgets = _open_budgets(db, auction, league.draft_rounds)
    if not open_budgets:
        return True
    if all(b.remaining_budget < MINIMUM_BID for b in open_budgets):
        return True
    if not available_assets(db, auction):
        return True
    return _unsold_streak(db, auction) >= len(open_budgets)


def _advance_nominator(db: Session, auction: AuctionDraft, roster_size: int) -> None:
    """Turn passes to the next team in order that still has a roster slot
    open - a full team has nothing left to buy, so it isn't put on the clock.
    Rotation is regardless of who won the last round, or whether anyone bid."""
    full = {
        b.team_id
        for b in db.query(AuctionTeamBudget).filter(AuctionTeamBudget.auction_draft_id == auction.id)
        if b.assets_won >= roster_size
    }
    n = len(auction.nomination_order)
    for step in range(1, n + 1):
        idx = (auction.next_nominator_index + step) % n
        if auction.nomination_order[idx] not in full:
            auction.next_nominator_index = idx
            return
    auction.next_nominator_index = (auction.next_nominator_index + 1) % n


def resolve_round(
    db: Session, auction_round_id: int, now: datetime.datetime | None = None, force: bool = False
) -> AuctionRound:
    """Reveals all sealed bids for the round, picks the highest (ties broken
    by random choice among the tied teams), debits the winner's budget,
    records a roster_entries row so the asset drops out of the board, and
    advances nomination to the next team in turn order that still has a roster
    slot open - regardless of who won, or whether anyone bid at all.

    Refuses to resolve before the bid window closes unless force=True (a
    commissioner override) - the timed window is the whole point of a
    sealed-bid round.
    """
    now = now or datetime.datetime.utcnow()
    round_ = db.get(AuctionRound, auction_round_id)
    if round_ is None:
        raise ValueError("Auction round not found")
    if round_.status != "open":
        raise ValueError("Round has already been resolved")
    if not force and not is_round_expired(round_, now):
        raise ValueError("Bid window is still open")

    auction = db.get(AuctionDraft, round_.auction_draft_id)
    bids = db.query(AuctionBid).filter(AuctionBid.auction_round_id == auction_round_id).all()

    if not bids:
        round_.status = "unsold"
    else:
        high_amount = max(b.amount for b in bids)
        tied = [b for b in bids if b.amount == high_amount]
        winner = tied[0] if len(tied) == 1 else random.choice(tied)

        budget = (
            db.query(AuctionTeamBudget)
            .filter(
                AuctionTeamBudget.auction_draft_id == auction.id,
                AuctionTeamBudget.team_id == winner.team_id,
            )
            .first()
        )
        budget.remaining_budget -= winner.amount
        budget.assets_won += 1

        round_.status = "resolved"
        round_.winning_team_id = winner.team_id
        round_.winning_bid_amount = winner.amount
        round_.was_tie_broken_randomly = len(tied) > 1

        db.add(
            RosterEntry(
                team_id=winner.team_id, draftable_asset_id=round_.draftable_asset_id, acquired_via="auction"
            )
        )

    round_.resolved_at = now
    db.flush()  # winner's assets_won / unsold status must be visible to the queries below
    _advance_nominator(db, auction, db.get(League, auction.league_id).draft_rounds)
    if _is_auction_complete(db, auction):
        auction.status = "complete"
        auction.completed_at = now

    db.flush()
    return round_
