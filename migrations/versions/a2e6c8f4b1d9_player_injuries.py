"""player_injuries

players.injury_status / injury_note / injury_updated_at: the real-world injury report.
roster_entries.on_injured_list: the player sits on his fantasy team's injured list.

Revision ID: a2e6c8f4b1d9
Revises: d8a2b6e0c4f1
Create Date: 2026-10-15 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a2e6c8f4b1d9"
down_revision: Union[str, Sequence[str], None] = "d8a2b6e0c4f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("players", sa.Column("injury_status", sa.String(length=10), nullable=True))
    op.add_column("players", sa.Column("injury_note", sa.String(length=200), nullable=True))
    op.add_column("players", sa.Column("injury_updated_at", sa.DateTime(), nullable=True))
    op.add_column("roster_entries", sa.Column("on_injured_list", sa.Boolean(), server_default="0", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("roster_entries") as batch:
        batch.drop_column("on_injured_list")
    with op.batch_alter_table("players") as batch:
        batch.drop_column("injury_updated_at")
        batch.drop_column("injury_note")
        batch.drop_column("injury_status")
