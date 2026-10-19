"""watchlist

Players a team is watching (the Watch button on the player card): watchlist_entries.

Revision ID: d5f1a7c3e9b2
Revises: c4e8a2d6f1b9
Create Date: 2026-10-25 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5f1a7c3e9b2"
down_revision: Union[str, Sequence[str], None] = "c4e8a2d6f1b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "watchlist_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), sa.ForeignKey("draftable_assets.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("team_id", "draftable_asset_id", name="uq_watchlist_team_asset"),
    )
    op.create_index("ix_watchlist_entries_team_id", "watchlist_entries", ["team_id"])


def downgrade() -> None:
    op.drop_index("ix_watchlist_entries_team_id", table_name="watchlist_entries")
    op.drop_table("watchlist_entries")
