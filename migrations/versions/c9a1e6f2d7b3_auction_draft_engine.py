"""auction_draft_engine

Revision ID: c9a1e6f2d7b3
Revises: b385f5a3bd16
Create Date: 2026-09-17 20:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9a1e6f2d7b3'
down_revision: Union[str, Sequence[str], None] = 'b385f5a3bd16'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # program_size drives a team's auction starting budget (see
    # platform/auction/constants.py). Default 'small' matches the future
    # dynamic-mode rule that a program starts small and builds up over
    # multiple seasons (not implemented yet).
    with op.batch_alter_table("teams") as batch_op:
        batch_op.add_column(
            sa.Column("program_size", sa.String(length=20), nullable=False, server_default="small")
        )
        batch_op.create_check_constraint(
            "ck_team_program_size", "program_size IN ('large', 'moderate', 'small')"
        )

    op.create_table(
        "auction_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("league_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("round_seconds", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("nomination_order", sa.JSON(), nullable=False),
        sa.Column("next_nominator_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "auction_team_budgets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("auction_draft_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("starting_budget", sa.Integer(), nullable=False),
        sa.Column("remaining_budget", sa.Integer(), nullable=False),
        sa.Column("assets_won", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["auction_draft_id"], ["auction_drafts.id"], ),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("auction_draft_id", "team_id"),
    )

    op.create_table(
        "auction_rounds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("auction_draft_id", sa.Integer(), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), nullable=False),
        sa.Column("nominated_by_team_id", sa.Integer(), nullable=False),
        sa.Column("opens_at", sa.DateTime(), nullable=False),
        sa.Column("closes_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("winning_team_id", sa.Integer(), nullable=True),
        sa.Column("winning_bid_amount", sa.Integer(), nullable=True),
        sa.Column("was_tie_broken_randomly", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["auction_draft_id"], ["auction_drafts.id"], ),
        sa.ForeignKeyConstraint(["draftable_asset_id"], ["draftable_assets.id"], ),
        sa.ForeignKeyConstraint(["nominated_by_team_id"], ["teams.id"], ),
        sa.ForeignKeyConstraint(["winning_team_id"], ["teams.id"], ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("auction_draft_id", "round_number"),
    )

    op.create_table(
        "auction_bids",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("auction_round_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["auction_round_id"], ["auction_rounds.id"], ),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("auction_round_id", "team_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("auction_bids")
    op.drop_table("auction_rounds")
    op.drop_table("auction_team_budgets")
    op.drop_table("auction_drafts")

    with op.batch_alter_table("teams") as batch_op:
        batch_op.drop_constraint("ck_team_program_size", type_="check")
        batch_op.drop_column("program_size")
