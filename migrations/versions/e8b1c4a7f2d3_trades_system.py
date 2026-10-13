"""trades_system

Adds trades, trade_items, and trade_votes tables for multi-team and AI-approval trading.

Revision ID: e8b1c4a7f2d3
Revises: d6f0b4c8e2a5
Create Date: 2026-10-13 15:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8b1c4a7f2d3"
down_revision: Union[str, Sequence[str], None] = "d6f0b4c8e2a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("league_id", sa.Integer(), nullable=False),
        sa.Column("proposer_team_id", sa.Integer(), nullable=False),
        sa.Column("counterparty_team_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="proposed"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.ForeignKeyConstraint(["proposer_team_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["counterparty_team_id"], ["teams.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("trades") as batch_op:
        batch_op.create_index("ix_trades_league_id", ["league_id"])
        batch_op.create_index("ix_trades_proposer_team_id", ["proposer_team_id"])
        batch_op.create_index("ix_trades_counterparty_team_id", ["counterparty_team_id"])

    op.create_table(
        "trade_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trade_id", sa.Integer(), nullable=False),
        sa.Column("sender_team_id", sa.Integer(), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["trade_id"], ["trades.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sender_team_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["draftable_asset_id"], ["draftable_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("trade_items") as batch_op:
        batch_op.create_index("ix_trade_items_trade_id", ["trade_id"])

    op.create_table(
        "trade_votes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trade_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("vote", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["trade_id"], ["trades.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trade_id", "team_id", name="uq_trade_vote_team"),
    )
    with op.batch_alter_table("trade_votes") as batch_op:
        batch_op.create_index("ix_trade_votes_trade_id", ["trade_id"])


def downgrade() -> None:
    op.drop_table("trade_votes")
    op.drop_table("trade_items")
    op.drop_table("trades")
