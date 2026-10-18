"""
Offline Draft Mode tables (platform/offline_draft.py): a draft run in the room (or hybrid) with the platform as the single
source of truth. Snake picks and auction sales share one table (OfflineDraftPick); undo is a void, never a delete.
"""

from __future__ import annotations

import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from fantasy_sports.platform.db.base import Base


class OfflineDraftSession(Base):
    __tablename__ = "offline_draft_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), index=True)
    format: Mapped[str] = mapped_column(String(10))                      # snake | auction
    join_code: Mapped[str] = mapped_column(String(8), unique=True)
    display_token: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(12), default="setup")      # setup/live/paused/review/finalized
    entry_mode: Mapped[str] = mapped_column(String(6), default="free")    # live | free
    # rounds, team_order, cell_owners, budget, min_bid, increment, pool (asset ids snapshotted at creation), ...
    settings_json: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=0)              # bumped on every state change
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    finalized_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)


class OfflineTeamClaim(Base):
    """A team's seat at the session (phone join flow, phase 3)."""

    __tablename__ = "offline_team_claims"
    __table_args__ = (UniqueConstraint("session_id", "team_id", name="uq_offline_claim"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("offline_draft_sessions.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    pin_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    claimed_device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="unclaimed")  # unclaimed/pending/claimed
    failed_pins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class OfflineDraftPick(Base):
    """A snake pick or an auction sale. board_round/board_slot is WHERE it sits on the board; entry_seq is WHEN it was
    entered. The two are independent, so the board can be filled in any order and the history stays true."""

    __tablename__ = "offline_draft_picks"
    __table_args__ = (
        # one live pick per player, and one per board cell, per session (voided rows don't count)
        Index("uq_offline_pick_asset", "session_id", "draftable_asset_id", unique=True,
              sqlite_where=text("voided_at IS NULL"), postgresql_where=text("voided_at IS NULL")),
        Index("uq_offline_pick_cell", "session_id", "board_round", "board_slot", unique=True,
              sqlite_where=text("voided_at IS NULL AND board_round IS NOT NULL"),
              postgresql_where=text("voided_at IS NULL AND board_round IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("offline_draft_sessions.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    board_round: Mapped[int | None] = mapped_column(Integer, nullable=True)
    board_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_keeper: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    entry_seq: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(12))                       # phone/commissioner/upload/autopick
    entered_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    entered_by_device: Mapped[str | None] = mapped_column(String(64), nullable=True)
    override: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    voided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    voided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)


class OfflineAuctionLot(Base):
    """The live lot (phase 3: phones as bidding paddles). One open at a time."""

    __tablename__ = "offline_auction_lots"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("offline_draft_sessions.id"), index=True)
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    nominated_by_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    current_bid: Mapped[int] = mapped_column(Integer, default=0)
    current_bidder_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    ends_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="open")      # open/sold/cancelled


class OfflineBidEvent(Base):
    __tablename__ = "offline_bid_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("offline_auction_lots.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    amount: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    accepted: Mapped[bool] = mapped_column(Boolean, default=True)
    reject_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)


class OfflineAuditLog(Base):
    __tablename__ = "offline_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("offline_draft_sessions.id"), index=True)
    actor: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(40))
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime)
