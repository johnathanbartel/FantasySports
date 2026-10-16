"""pick_trading_toggle

leagues.pick_trading_enabled: the commissioner's switch for draft pick trading. Also merges the two open heads
(standings_playoffs and player_game_toi), so a bare `alembic upgrade head` works again.

Revision ID: b6e3d8a2f1c4
Revises: a7c2e5d1b8f3, b9e4a1c7d3f6
Create Date: 2026-10-16 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b6e3d8a2f1c4"
down_revision: Union[str, Sequence[str], None] = ("a7c2e5d1b8f3", "b9e4a1c7d3f6")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("pick_trading_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.drop_column("pick_trading_enabled")
