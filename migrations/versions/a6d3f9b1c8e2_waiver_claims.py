"""waiver_claims

waiver_claims: a team's claim on a player who is on waivers for 24 hours after being dropped.

Revision ID: a6d3f9b1c8e2
Revises: d8a2b6e0c4f1
Create Date: 2026-10-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a6d3f9b1c8e2"
down_revision: Union[str, Sequence[str], None] = "d8a2b6e0c4f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "waiver_claims",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), sa.ForeignKey("draftable_assets.id"), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.String(200), nullable=True),
    )
    op.create_index("ix_waiver_claims_league_id", "waiver_claims", ["league_id"])


def downgrade() -> None:
    op.drop_index("ix_waiver_claims_league_id", table_name="waiver_claims")
    op.drop_table("waiver_claims")
