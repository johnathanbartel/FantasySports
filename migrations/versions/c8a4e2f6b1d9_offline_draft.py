"""offline_draft

Offline Draft Mode: sessions, team claims, picks / sales (with void), auction lots and bids, audit log.

Revision ID: c8a4e2f6b1d9
Revises: b3f8c1d5a7e2
Create Date: 2026-10-18 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8a4e2f6b1d9"
down_revision: Union[str, Sequence[str], None] = "b3f8c1d5a7e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "offline_draft_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("format", sa.String(10), nullable=False),
        sa.Column("join_code", sa.String(8), nullable=False, unique=True),
        sa.Column("display_token", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("entry_mode", sa.String(6), nullable=False),
        sa.Column("settings_json", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finalized_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_offline_draft_sessions_league_id", "offline_draft_sessions", ["league_id"])

    op.create_table(
        "offline_team_claims",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("offline_draft_sessions.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("pin_hash", sa.String(200), nullable=True),
        sa.Column("claimed_device_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("failed_pins", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("session_id", "team_id", name="uq_offline_claim"),
    )
    op.create_index("ix_offline_team_claims_session_id", "offline_team_claims", ["session_id"])

    op.create_table(
        "offline_draft_picks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("offline_draft_sessions.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), sa.ForeignKey("draftable_assets.id"), nullable=False),
        sa.Column("board_round", sa.Integer(), nullable=True),
        sa.Column("board_slot", sa.Integer(), nullable=True),
        sa.Column("price", sa.Integer(), nullable=True),
        sa.Column("is_keeper", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("entry_seq", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(12), nullable=False),
        sa.Column("entered_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("entered_by_device", sa.String(64), nullable=True),
        sa.Column("override", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("voided_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("void_reason", sa.String(200), nullable=True),
    )
    op.create_index("ix_offline_draft_picks_session_id", "offline_draft_picks", ["session_id"])
    live = sa.text("voided_at IS NULL")
    op.create_index("uq_offline_pick_asset", "offline_draft_picks", ["session_id", "draftable_asset_id"], unique=True,
                    sqlite_where=live, postgresql_where=live)
    cell = sa.text("voided_at IS NULL AND board_round IS NOT NULL")
    op.create_index("uq_offline_pick_cell", "offline_draft_picks", ["session_id", "board_round", "board_slot"], unique=True,
                    sqlite_where=cell, postgresql_where=cell)

    op.create_table(
        "offline_auction_lots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("offline_draft_sessions.id"), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), sa.ForeignKey("draftable_assets.id"), nullable=False),
        sa.Column("nominated_by_team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("current_bid", sa.Integer(), nullable=False),
        sa.Column("current_bidder_team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=True),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
    )
    op.create_index("ix_offline_auction_lots_session_id", "offline_auction_lots", ["session_id"])

    op.create_table(
        "offline_bid_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lot_id", sa.Integer(), sa.ForeignKey("offline_auction_lots.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(12), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("reject_reason", sa.String(200), nullable=True),
    )
    op.create_index("ix_offline_bid_events_lot_id", "offline_bid_events", ["lot_id"])

    op.create_table(
        "offline_audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("offline_draft_sessions.id"), nullable=False),
        sa.Column("actor", sa.String(60), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_offline_audit_log_session_id", "offline_audit_log", ["session_id"])


def downgrade() -> None:
    for t in ("offline_audit_log", "offline_bid_events", "offline_auction_lots", "offline_draft_picks",
              "offline_team_claims", "offline_draft_sessions"):
        op.drop_table(t)
