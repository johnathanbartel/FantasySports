"""recruiting_drafts

Revision ID: f6c4e0b8d2a3
Revises: e5b3d9a7c1f2
Create Date: 2026-09-20 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f6c4e0b8d2a3'
down_revision: Union[str, Sequence[str], None] = 'e5b3d9a7c1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recruiting_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("league_id", sa.Integer(), nullable=False),
        sa.Column("period_seconds", sa.Integer(), nullable=False),
        sa.Column("buffer_seconds", sa.Integer(), nullable=False, server_default="20"),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("league_id"),
    )
    op.create_table(
        "recruiting_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("league_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("period_seconds", sa.Integer(), nullable=False),
        sa.Column("buffer_seconds", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("total_periods", sa.Integer(), nullable=False),
        sa.Column("periods_processed", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("league_id"),
    )
    op.create_table(
        "recruiting_thresholds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.Integer(), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("is_elite", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["recruiting_drafts.id"]),
        sa.ForeignKeyConstraint(["draftable_asset_id"], ["draftable_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id", "draftable_asset_id"),
    )
    op.create_table(
        "recruiting_bids",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["recruiting_drafts.id"]),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["draftable_asset_id"], ["draftable_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id", "team_id", "draftable_asset_id"),
    )
    op.create_table(
        "recruiting_awards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.Integer(), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("period_number", sa.Integer(), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column("was_tie_broken_randomly", sa.Boolean(), nullable=False),
        sa.Column("awarded_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["recruiting_drafts.id"]),
        sa.ForeignKeyConstraint(["draftable_asset_id"], ["draftable_assets.id"]),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id", "draftable_asset_id"),
    )
    op.create_table(
        "recruiting_charges",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), nullable=False),
        sa.Column("period_number", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("is_winner", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["recruiting_drafts.id"]),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["draftable_asset_id"], ["draftable_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id", "team_id", "draftable_asset_id"),
    )
    op.create_table(
        "recruiting_bid_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.Integer(), nullable=False),
        sa.Column("period_number", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), nullable=False),
        sa.Column("bid_amount", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=10), nullable=False),
        sa.Column("completion_pct", sa.Float(), nullable=False),
        sa.Column("charged", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["recruiting_drafts.id"]),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["draftable_asset_id"], ["draftable_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id", "period_number", "team_id", "draftable_asset_id"),
    )
    op.create_table(
        "recruiting_result_acks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("period_number", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["recruiting_drafts.id"]),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id", "team_id", "period_number"),
    )
    op.create_table(
        "user_entitlements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("feature", sa.String(length=40), nullable=False),
        sa.Column("granted_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "feature"),
    )
    op.create_table(
        "free_agency_transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("league_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["draftable_asset_id"], ["draftable_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_free_agency_transactions_league_id", "free_agency_transactions", ["league_id"])


def downgrade() -> None:
    op.drop_index("ix_free_agency_transactions_league_id", table_name="free_agency_transactions")
    op.drop_table("free_agency_transactions")
    op.drop_table("user_entitlements")
    op.drop_table("recruiting_result_acks")
    op.drop_table("recruiting_bid_results")
    op.drop_table("recruiting_charges")
    op.drop_table("recruiting_awards")
    op.drop_table("recruiting_bids")
    op.drop_table("recruiting_thresholds")
    op.drop_table("recruiting_drafts")
    op.drop_table("recruiting_settings")
