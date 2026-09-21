"""
Timed Auction Draft schema: the Recruiting Period, its per-slot rounds, the
blind bids submitted into each round, and the tiered budgets those bids are
debited from.

This is sport-agnostic on purpose. Nothing here encodes football: the number
of rounds, the per-round window, the tier codes, and the tier budgets are all
stored as data on the session (see platform/auction/config.py for the config
objects that populate them, and sports/football/college/auction.py for the
NCAAF values). A hockey or basketball league gets its own rows here, not a
schema change.

These are new tables rather than extra columns on `drafts`/`draft_picks`: a
snake draft and an auction share the *outcome* (a team ends up owning an
asset) but not the mechanics, so the auction keeps its own bid/round history
and writes its results back into the existing draft tables when a round
resolves. That way the already-built draft board, pick history, and roster
endpoints keep working unchanged.

Defensive position groups are bid on individually, per group - never bundled
by real team (docs/data_contract.md, "Decision: Timed Auction Draft defensive
position bidding granularity"). That decision is why there is no bundle table
here: the atomic bidding unit is a single `draftable_assets` row, which for
defense is already one `defense_groups` row (one real team, one of DL/LB/DB).
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fantasy_sports.platform.db.base import Base


class AuctionSession(Base):
    """One Recruiting Period - the auction equivalent of a snake draft run.

    Hangs off an existing `drafts` row so everything downstream of a draft
    (pick history, rosters, the draft board's "what's left" query) is shared
    with the snake path instead of duplicated.

    `round_seconds` is the per-slot bidding window and `minimum_round_seconds`
    is the floor it may not go below (3 minutes platform-wide today, but a
    sport can raise its own floor without a code change).
    """

    __tablename__ = "auction_sessions"
    __table_args__ = (
        UniqueConstraint("draft_id"),
        CheckConstraint("total_rounds > 0", name="ck_auction_session_rounds_positive"),
        CheckConstraint(
            "round_seconds >= minimum_round_seconds",
            name="ck_auction_session_round_window_floor",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id"))
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    # Same vocabulary as Draft.status: pending/in_progress/complete.
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # One round per roster slot - the Recruiting Period is divided per slot.
    total_rounds: Mapped[int] = mapped_column(Integer)
    round_seconds: Mapped[int] = mapped_column(Integer)
    minimum_round_seconds: Mapped[int] = mapped_column(Integer, default=180)
    # Smallest legal bid; 1 unless a league configures otherwise.
    min_bid: Mapped[int] = mapped_column(Integer, default=1)
    default_tier_code: Mapped[str] = mapped_column(String(20))
    current_round_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    starts_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    tiers: Mapped[list["AuctionTier"]] = relationship(back_populates="session")
    budgets: Mapped[list["AuctionBudget"]] = relationship(back_populates="session")
    rounds: Mapped[list["AuctionRound"]] = relationship(back_populates="session")


class AuctionTier(Base):
    """A budget tier (program size: Large/Moderate/Small for college football).

    Stored per session rather than per league so a session is a self-contained
    snapshot - editing a league's tier table next season can't rewrite what a
    past auction actually ran with. Tier codes and budgets are data, so a
    sport with different tier names supplies different rows.
    """

    __tablename__ = "auction_tiers"
    __table_args__ = (
        UniqueConstraint("auction_session_id", "code"),
        CheckConstraint("starting_budget > 0", name="ck_auction_tier_budget_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    auction_session_id: Mapped[int] = mapped_column(ForeignKey("auction_sessions.id"))
    code: Mapped[str] = mapped_column(String(20))  # "large"/"moderate"/"small"
    display_name: Mapped[str] = mapped_column(String(60))
    starting_budget: Mapped[int] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    session: Mapped["AuctionSession"] = relationship(back_populates="tiers")


class AuctionBudget(Base):
    """One fantasy team's wallet for one auction session."""

    __tablename__ = "auction_budgets"
    __table_args__ = (
        UniqueConstraint("auction_session_id", "team_id"),
        CheckConstraint("spent_budget >= 0", name="ck_auction_budget_spent_non_negative"),
        CheckConstraint(
            "spent_budget <= starting_budget", name="ck_auction_budget_not_overspent"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    auction_session_id: Mapped[int] = mapped_column(ForeignKey("auction_sessions.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    auction_tier_id: Mapped[int] = mapped_column(ForeignKey("auction_tiers.id"))
    # Copied off the tier at session creation so a later tier edit can't
    # retroactively change what a team was actually given.
    starting_budget: Mapped[int] = mapped_column(Integer)
    spent_budget: Mapped[int] = mapped_column(Integer, default=0)

    session: Mapped["AuctionSession"] = relationship(back_populates="budgets")
    tier: Mapped["AuctionTier"] = relationship()

    @property
    def remaining_budget(self) -> int:
        return self.starting_budget - self.spent_budget


class AuctionRound(Base):
    """One per-slot round of the Recruiting Period.

    Windows are absolute timestamps rather than a duration offset so a round
    that resolves early can shift everything after it without the client
    recomputing anything.
    """

    __tablename__ = "auction_rounds"
    __table_args__ = (
        UniqueConstraint("auction_session_id", "round_number"),
        CheckConstraint("round_number > 0", name="ck_auction_round_number_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    auction_session_id: Mapped[int] = mapped_column(ForeignKey("auction_sessions.id"))
    round_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/open/resolved
    opens_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    closes_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    session: Mapped["AuctionSession"] = relationship(back_populates="rounds")


class AuctionBid(Base):
    """One blind bid: one owner, one round, one draftable asset, one amount.

    The unique constraint is the "one blind bid per round" rule made
    structural. Blindness itself is an API concern (the round endpoint hides
    other owners' bids until the round resolves), not a storage concern.
    """

    __tablename__ = "auction_bids"
    __table_args__ = (
        UniqueConstraint("auction_round_id", "team_id"),
        CheckConstraint("amount > 0", name="ck_auction_bid_amount_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    auction_session_id: Mapped[int] = mapped_column(ForeignKey("auction_sessions.id"))
    auction_round_id: Mapped[int] = mapped_column(ForeignKey("auction_rounds.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="submitted")  # submitted/won/lost
    # "outbid" / "tie_break" / "insufficient_budget" / "already_drafted"
    lost_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    submitted_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

    draftable_asset: Mapped["DraftableAsset"] = relationship()  # noqa: F821


class AuctionRoundResult(Base):
    """One resolved contest - a single asset that drew at least one bid in a
    round, and who ended up with it.

    A round has as many results as there were distinct assets bid on, not one:
    every owner bids on whoever they want, so twelve owners can win twelve
    different assets in the same round, or all pile onto one asset and produce
    a single result with eleven losers.
    """

    __tablename__ = "auction_round_results"
    __table_args__ = (UniqueConstraint("auction_round_id", "draftable_asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    auction_round_id: Mapped[int] = mapped_column(ForeignKey("auction_rounds.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    winning_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    winning_amount: Mapped[int] = mapped_column(Integer)
    bid_count: Mapped[int] = mapped_column(Integer)
    # True when the top bid was tied and the asset made a random "player
    # choice" between the tied owners.
    tie_break_used: Mapped[bool] = mapped_column(Boolean, default=False)
    tied_team_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    draft_pick_id: Mapped[int | None] = mapped_column(ForeignKey("draft_picks.id"), nullable=True)

    draftable_asset: Mapped["DraftableAsset"] = relationship()  # noqa: F821
