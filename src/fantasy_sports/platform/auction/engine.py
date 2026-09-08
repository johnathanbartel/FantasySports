"""
The Timed Auction Draft round resolution engine.

One full cycle, which `resolve_round` performs atomically:

    bid     owners submit one blind bid per round from the revealed pool
            (`submit_bid`); nobody can see anyone else's bid while the
            round's window is open
    reveal  when the window closes every bid in the round is revealed at once
    debit   for each asset bid on, the highest bidder wins it, their budget is
            debited by exactly their bid, and the asset is written into the
            existing draft tables (a `draft_picks` row plus a `roster_entries`
            row) so it leaves the pool
    advance the next per-slot round opens and the session moves on, or the
            session completes if that was the last slot

Ties on the top bid are broken by random "player choice": the contested
player or defense group randomly picks one of the tied owners. The random
source is injected (`rng`) so the behaviour is deterministic under test.

Sport neutrality: this module reads round counts, window lengths, tier
budgets, and the minimum bid off the `AuctionSession`/`AuctionTier` rows,
never from constants. See platform/auction/config.py for where those values
come from.

Defensive position groups are bid on individually, per group, never bundled
by real team - see docs/data_contract.md, "Decision: Timed Auction Draft
defensive position bidding granularity". Concretely: the bidding unit is one
`draftable_assets` row, so Georgia DL, Georgia LB and Georgia DB are three
independent auction lines that different owners can win. There is deliberately
no bundling step anywhere in this file.
"""

from __future__ import annotations

import datetime
import random
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from fantasy_sports.platform.auction.config import AuctionConfig, plan_round_windows
from fantasy_sports.platform.db.models.auction import (
    AuctionBid,
    AuctionBudget,
    AuctionRound,
    AuctionRoundResult,
    AuctionSession,
    AuctionTier,
)
from fantasy_sports.platform.db.models.core import (
    Draft,
    DraftPick,
    DraftableAsset,
    League,
    LeagueUniverse,
    RosterEntry,
    Team,
)


class AuctionError(Exception):
    """A rule violation the caller can fix (bad bid, closed round, ...).

    Carries an HTTP status so the API layer can translate without a second
    mapping table.
    """

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ResolvedContest:
    """One asset's outcome inside a resolved round."""

    draftable_asset_id: int
    winning_team_id: int
    winning_amount: int
    bid_count: int
    tie_break_used: bool
    tied_team_ids: list[int]


@dataclass
class RoundResolution:
    """Everything the reveal step produced for one round."""

    round_number: int
    contests: list[ResolvedContest] = field(default_factory=list)
    losing_bid_ids: list[int] = field(default_factory=list)
    next_round_number: int | None = None
    session_complete: bool = False


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


# --- Session setup ----------------------------------------------------------


def create_session(
    db: Session,
    *,
    league_id: int,
    config: AuctionConfig,
    team_tier_codes: dict[int, str] | None = None,
    starts_at: datetime.datetime | None = None,
    now: datetime.datetime | None = None,
) -> AuctionSession:
    """Create a Recruiting Period for a league and plan every round window.

    Creates the backing `drafts` row too, so auction results land in the same
    draft/roster tables the snake path already uses.
    """
    now = now or _utcnow()
    starts_at = starts_at or now
    team_tier_codes = team_tier_codes or {}

    league = db.get(League, league_id)
    if league is None:
        raise AuctionError("League not found", status_code=404)

    teams = db.query(Team).filter(Team.league_id == league_id).order_by(Team.id).all()
    if not teams:
        raise AuctionError("League has no teams yet")

    team_ids = {t.id for t in teams}
    unknown = sorted(set(team_tier_codes) - team_ids)
    if unknown:
        raise AuctionError(f"Team(s) {unknown} are not in league {league_id}")

    round_seconds = config.resolved_round_seconds()

    draft = Draft(league_id=league_id, status="in_progress", started_at=now)
    db.add(draft)
    db.flush()

    session = AuctionSession(
        draft_id=draft.id,
        league_id=league_id,
        status="pending",
        total_rounds=config.slots,
        round_seconds=round_seconds,
        minimum_round_seconds=config.minimum_round_seconds,
        min_bid=config.min_bid,
        default_tier_code=config.default_tier_code,
        starts_at=starts_at,
    )
    db.add(session)
    db.flush()

    tier_rows: dict[str, AuctionTier] = {}
    for tier in config.tiers:
        row = AuctionTier(
            auction_session_id=session.id,
            code=tier.code,
            display_name=tier.display_name,
            starting_budget=tier.starting_budget,
            sort_order=tier.sort_order,
        )
        db.add(row)
        tier_rows[tier.code] = row
    db.flush()

    for team in teams:
        code = team_tier_codes.get(team.id, config.default_tier_code)
        if code not in tier_rows:
            raise AuctionError(
                f"Unknown tier {code!r} for team {team.id}; "
                f"configured tiers are {sorted(tier_rows)}"
            )
        tier_row = tier_rows[code]
        db.add(
            AuctionBudget(
                auction_session_id=session.id,
                team_id=team.id,
                auction_tier_id=tier_row.id,
                starting_budget=tier_row.starting_budget,
                spent_budget=0,
            )
        )

    for window in plan_round_windows(
        starts_at, config.slots, round_seconds, config.minimum_round_seconds
    ):
        db.add(
            AuctionRound(
                auction_session_id=session.id,
                round_number=window.round_number,
                status="pending",
                opens_at=window.opens_at,
                closes_at=window.closes_at,
            )
        )
    db.flush()

    ensure_started(db, session, now=now)
    return session


def ensure_started(
    db: Session, session: AuctionSession, now: datetime.datetime | None = None
) -> AuctionSession:
    """Flip a scheduled session to running once its start time has arrived.

    A Recruiting Period can be created ahead of time; round 1 doesn't open
    until `starts_at`.
    """
    now = now or _utcnow()
    if session.status != "pending":
        return session
    if session.starts_at is not None and now < session.starts_at:
        return session

    session.status = "in_progress"
    session.started_at = now
    first = _round(db, session, 1)
    _open_round(db, session, first, opens_at=now)
    db.flush()
    return session


# --- Internals --------------------------------------------------------------


def _round(db: Session, session: AuctionSession, round_number: int) -> AuctionRound:
    row = (
        db.query(AuctionRound)
        .filter(
            AuctionRound.auction_session_id == session.id,
            AuctionRound.round_number == round_number,
        )
        .one_or_none()
    )
    if row is None:
        raise AuctionError(f"Round {round_number} does not exist", status_code=404)
    return row


def _open_round(
    db: Session, session: AuctionSession, round_row: AuctionRound, opens_at: datetime.datetime
) -> None:
    """Open a round now and slide every later pending round in behind it.

    Rounds tile the Recruiting Period back to back, so a round that opens
    early or late moves the whole tail rather than leaving a gap.
    """
    round_row.status = "open"
    round_row.opens_at = opens_at
    round_row.closes_at = opens_at + datetime.timedelta(seconds=session.round_seconds)
    session.current_round_number = round_row.round_number

    later = (
        db.query(AuctionRound)
        .filter(
            AuctionRound.auction_session_id == session.id,
            AuctionRound.round_number > round_row.round_number,
        )
        .order_by(AuctionRound.round_number)
        .all()
    )
    cursor = round_row.closes_at
    for row in later:
        row.opens_at = cursor
        cursor = row.closes_at = cursor + datetime.timedelta(seconds=session.round_seconds)


def _budget(db: Session, session: AuctionSession, team_id: int) -> AuctionBudget:
    row = (
        db.query(AuctionBudget)
        .filter(
            AuctionBudget.auction_session_id == session.id,
            AuctionBudget.team_id == team_id,
        )
        .one_or_none()
    )
    if row is None:
        raise AuctionError(f"Team {team_id} is not in this auction", status_code=404)
    return row


def drafted_asset_ids(db: Session, session: AuctionSession) -> set[int]:
    """Assets already won in this session - i.e. off the revealed pool."""
    return {
        pick.draftable_asset_id
        for pick in db.query(DraftPick).filter(
            DraftPick.draft_id == session.draft_id,
            DraftPick.draftable_asset_id.isnot(None),
        )
    }


def _league_universe_ids(db: Session, session: AuctionSession) -> list[int]:
    return [
        lu.universe_id
        for lu in db.query(LeagueUniverse).filter(LeagueUniverse.league_id == session.league_id)
    ]


def _next_pick_number(db: Session, session: AuctionSession) -> int:
    used = db.query(DraftPick).filter(DraftPick.draft_id == session.draft_id).count()
    return used + 1


def player_choice(rng: random.Random, tied_bids: list[AuctionBid]) -> AuctionBid:
    """The tie-break: the contested player/asset picks an owner at random.

    Candidates are sorted by team id first so a seeded `rng` gives the same
    answer every run regardless of the order bids came back from the database.
    """
    ordered = sorted(tied_bids, key=lambda b: b.team_id)
    return rng.choice(ordered)


# --- Bidding ----------------------------------------------------------------


def current_round(db: Session, session: AuctionSession) -> AuctionRound | None:
    if session.current_round_number is None:
        return None
    return _round(db, session, session.current_round_number)


def submit_bid(
    db: Session,
    session: AuctionSession,
    *,
    team_id: int,
    draftable_asset_id: int,
    amount: int,
    now: datetime.datetime | None = None,
) -> AuctionBid:
    """Record one owner's blind bid for the open round.

    Re-submitting replaces that owner's bid: while the window is open a bid is
    private, so changing it is not information anyone else could have acted
    on. One bid per owner per round is enforced by the table's unique
    constraint as well as here.
    """
    now = now or _utcnow()
    ensure_started(db, session, now=now)

    if session.status == "complete":
        raise AuctionError("This auction has already finished", status_code=409)
    if session.status != "in_progress":
        raise AuctionError(
            f"Auction has not started yet (starts at {session.starts_at})", status_code=409
        )

    round_row = current_round(db, session)
    if round_row is None or round_row.status != "open":
        raise AuctionError("No round is open for bidding", status_code=409)
    if now >= round_row.closes_at:
        raise AuctionError(
            f"Round {round_row.round_number} closed at {round_row.closes_at}", status_code=409
        )

    budget = _budget(db, session, team_id)

    if amount < session.min_bid:
        raise AuctionError(f"Minimum bid is {session.min_bid}")
    if amount > budget.remaining_budget:
        raise AuctionError(
            f"Bid of {amount} exceeds team {team_id}'s remaining budget "
            f"of {budget.remaining_budget}"
        )

    asset = db.get(DraftableAsset, draftable_asset_id)
    if asset is None:
        raise AuctionError("Draftable asset not found", status_code=404)
    if asset.universe_id not in _league_universe_ids(db, session):
        raise AuctionError("Asset is not in this league's universe(s)")
    if draftable_asset_id in drafted_asset_ids(db, session):
        raise AuctionError("That asset has already been won in this auction", status_code=409)

    bid = (
        db.query(AuctionBid)
        .filter(
            AuctionBid.auction_round_id == round_row.id,
            AuctionBid.team_id == team_id,
        )
        .one_or_none()
    )
    if bid is None:
        bid = AuctionBid(
            auction_session_id=session.id,
            auction_round_id=round_row.id,
            team_id=team_id,
            draftable_asset_id=draftable_asset_id,
            amount=amount,
            status="submitted",
            submitted_at=now,
        )
        db.add(bid)
    else:
        bid.draftable_asset_id = draftable_asset_id
        bid.amount = amount
        bid.status = "submitted"
        bid.lost_reason = None
        bid.submitted_at = now

    db.flush()
    return bid


# --- Resolution -------------------------------------------------------------


def resolve_round(
    db: Session,
    session: AuctionSession,
    *,
    round_number: int | None = None,
    rng: random.Random | None = None,
    now: datetime.datetime | None = None,
    force: bool = False,
) -> RoundResolution:
    """Reveal, settle, and advance past one round.

    `force=True` settles a round whose window hasn't expired yet - the
    commissioner shortcut for "everyone has bid, move on". Without it, a round
    can only be resolved once its window has closed.
    """
    now = now or _utcnow()
    rng = rng or random.Random()

    if session.status == "complete":
        raise AuctionError("This auction has already finished", status_code=409)
    if session.status != "in_progress":
        raise AuctionError("Auction has not started yet", status_code=409)

    if round_number is None:
        round_number = session.current_round_number
    if round_number is None:
        raise AuctionError("No round is open", status_code=409)

    round_row = _round(db, session, round_number)
    if round_row.status == "resolved":
        raise AuctionError(f"Round {round_number} is already resolved", status_code=409)
    if round_row.status != "open":
        raise AuctionError(f"Round {round_number} has not opened yet", status_code=409)
    if not force and now < round_row.closes_at:
        raise AuctionError(
            f"Round {round_number} is still open for bidding until {round_row.closes_at}",
            status_code=409,
        )

    bids = (
        db.query(AuctionBid)
        .filter(AuctionBid.auction_round_id == round_row.id)
        .order_by(AuctionBid.team_id)
        .all()
    )

    already_drafted = drafted_asset_ids(db, session)
    resolution = RoundResolution(round_number=round_number)

    # Reveal: every bid in the round is now visible at once. Screen out the
    # ones that can't be honoured before comparing amounts.
    live_by_asset: dict[int, list[AuctionBid]] = defaultdict(list)
    for bid in bids:
        if bid.draftable_asset_id in already_drafted:
            bid.status = "lost"
            bid.lost_reason = "already_drafted"
            resolution.losing_bid_ids.append(bid.id)
            continue
        if bid.amount > _budget(db, session, bid.team_id).remaining_budget:
            bid.status = "lost"
            bid.lost_reason = "insufficient_budget"
            resolution.losing_bid_ids.append(bid.id)
            continue
        live_by_asset[bid.draftable_asset_id].append(bid)

    winners: list[tuple[AuctionBid, list[AuctionBid], list[int]]] = []
    for asset_id in sorted(live_by_asset):
        contenders = live_by_asset[asset_id]
        top_amount = max(b.amount for b in contenders)
        tied = [b for b in contenders if b.amount == top_amount]
        tied_team_ids: list[int] = []
        if len(tied) > 1:
            tied_team_ids = sorted(b.team_id for b in tied)
            winning_bid = player_choice(rng, tied)
        else:
            winning_bid = tied[0]
        winners.append((winning_bid, contenders, tied_team_ids))

    # Highest price picks first, so pick_number ordering reads like a board.
    winners.sort(key=lambda w: (-w[0].amount, w[0].draftable_asset_id))
    pick_number = _next_pick_number(db, session)

    for winning_bid, contenders, tied_team_ids in winners:
        budget = _budget(db, session, winning_bid.team_id)
        # Debit: the winner pays exactly what they bid, losers pay nothing.
        budget.spent_budget += winning_bid.amount
        winning_bid.status = "won"
        winning_bid.lost_reason = None

        for bid in contenders:
            if bid.id == winning_bid.id:
                continue
            bid.status = "lost"
            bid.lost_reason = "tie_break" if bid.team_id in tied_team_ids else "outbid"
            resolution.losing_bid_ids.append(bid.id)

        pick = DraftPick(
            draft_id=session.draft_id,
            round=round_number,
            pick_number=pick_number,
            team_id=winning_bid.team_id,
            draftable_asset_id=winning_bid.draftable_asset_id,
            picked_at=now,
            is_autopick=False,
        )
        db.add(pick)
        db.flush()
        pick_number += 1

        # roster_entries is unique on (team, asset). A league that already ran
        # a draft can start a second one over the same pool (the snake path
        # allows this too), so the winner may already hold this asset - in
        # which case the roster is already right and there is nothing to add.
        already_rostered = (
            db.query(RosterEntry)
            .filter(
                RosterEntry.team_id == winning_bid.team_id,
                RosterEntry.draftable_asset_id == winning_bid.draftable_asset_id,
            )
            .first()
        )
        if already_rostered is None:
            db.add(
                RosterEntry(
                    team_id=winning_bid.team_id,
                    draftable_asset_id=winning_bid.draftable_asset_id,
                    acquired_via="auction",
                )
            )
        db.add(
            AuctionRoundResult(
                auction_round_id=round_row.id,
                draftable_asset_id=winning_bid.draftable_asset_id,
                winning_team_id=winning_bid.team_id,
                winning_amount=winning_bid.amount,
                bid_count=len(contenders),
                tie_break_used=bool(tied_team_ids),
                tied_team_ids=tied_team_ids or None,
                draft_pick_id=pick.id,
            )
        )
        resolution.contests.append(
            ResolvedContest(
                draftable_asset_id=winning_bid.draftable_asset_id,
                winning_team_id=winning_bid.team_id,
                winning_amount=winning_bid.amount,
                bid_count=len(contenders),
                tie_break_used=bool(tied_team_ids),
                tied_team_ids=tied_team_ids,
            )
        )

    round_row.status = "resolved"
    round_row.resolved_at = now

    # Advance: open the next per-slot round, or close the Recruiting Period.
    if round_number < session.total_rounds:
        next_row = _round(db, session, round_number + 1)
        _open_round(db, session, next_row, opens_at=now)
        resolution.next_round_number = next_row.round_number
    else:
        session.status = "complete"
        session.completed_at = now
        session.current_round_number = None
        draft = db.get(Draft, session.draft_id)
        if draft is not None:
            draft.status = "complete"
            draft.completed_at = now
        resolution.session_complete = True

    db.flush()
    return resolution
