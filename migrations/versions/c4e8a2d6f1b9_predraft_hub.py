"""pre-draft hub

Auto draft preference and personal player rankings per team: team_draft_prefs and team_draft_rankings.

Revision ID: c4e8a2d6f1b9
Revises: b2c6e9a4d8f1
Create Date: 2026-10-23 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4e8a2d6f1b9"
down_revision: Union[str, Sequence[str], None] = "b2c6e9a4d8f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "team_draft_prefs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False, unique=True),
        sa.Column("autodraft_mode", sa.String(14), nullable=False, server_default="fantasymotion"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("autodraft_mode IN ('fantasymotion', 'my_rankings')", name="ck_team_draft_prefs_mode"),
    )
    op.create_table(
        "team_draft_rankings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), sa.ForeignKey("draftable_assets.id"), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.UniqueConstraint("team_id", "draftable_asset_id"),
        sa.UniqueConstraint("team_id", "rank"),
    )
    op.create_index("ix_team_draft_rankings_team_id", "team_draft_rankings", ["team_id"])


def downgrade() -> None:
    op.drop_index("ix_team_draft_rankings_team_id", table_name="team_draft_rankings")
    op.drop_table("team_draft_rankings")
    op.drop_table("team_draft_prefs")
