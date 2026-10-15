"""injury_alerts

injury_alerts: a manager-facing notice that a rostered player's injury status changed.

Revision ID: b5c9e1a3d7f2
Revises: a2e6c8f4b1d9
Create Date: 2026-10-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b5c9e1a3d7f2"
down_revision: Union[str, Sequence[str], None] = "a2e6c8f4b1d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "injury_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), sa.ForeignKey("draftable_assets.id"), nullable=False),
        sa.Column("player_name", sa.String(length=100), nullable=False),
        sa.Column("old_status", sa.String(length=10), nullable=True),
        sa.Column("new_status", sa.String(length=10), nullable=True),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_injury_alerts_team_id", "injury_alerts", ["team_id"])


def downgrade() -> None:
    op.drop_index("ix_injury_alerts_team_id", table_name="injury_alerts")
    op.drop_table("injury_alerts")
