"""player_game_toi

Ice time per player per real NHL game (from the boxscore), so a starter who got no ice time in his counting game can be
replaced by the next player on the pecking order (docs/hockey_spec.md 6.5).

Revision ID: b9e4a1c7d3f6
Revises: a7c2e5d1b8f3
Create Date: 2026-10-17 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b9e4a1c7d3f6"
down_revision: Union[str, Sequence[str], None] = "a7c2e5d1b8f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "player_game_toi",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), nullable=False),
        sa.Column("toi_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"]),
        sa.ForeignKeyConstraint(["draftable_asset_id"], ["draftable_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "draftable_asset_id", name="uq_player_game_toi"),
    )
    op.create_index("ix_player_game_toi_game_id", "player_game_toi", ["game_id"])


def downgrade() -> None:
    op.drop_index("ix_player_game_toi_game_id", table_name="player_game_toi")
    op.drop_table("player_game_toi")
