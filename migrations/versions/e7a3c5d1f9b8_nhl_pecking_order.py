"""nhl_pecking_order

Ordered NHL depth chart (C/W/D/G priorities) per team.

Revision ID: e7a3c5d1f9b8
Revises: d2f6a8c1e4b7
Create Date: 2026-10-10 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7a3c5d1f9b8"
down_revision: Union[str, Sequence[str], None] = "d2f6a8c1e4b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "team_pecking_order",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("position_group", sa.String(length=1), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.CheckConstraint("position_group IN ('C', 'W', 'D', 'G')", name="ck_pecking_order_position_group"),
        sa.CheckConstraint(
            "(position_group IN ('C', 'G') AND priority BETWEEN 1 AND 4) OR "
            "(position_group IN ('W', 'D') AND priority BETWEEN 1 AND 8)",
            name="ck_pecking_order_priority_range",
        ),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["draftable_asset_id"], ["draftable_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", "position_group", "priority", name="uq_pecking_order_team_group_priority"),
        sa.UniqueConstraint("team_id", "draftable_asset_id", name="uq_pecking_order_team_asset"),
    )
    op.create_index("ix_pecking_order_team_id", "team_pecking_order", ["team_id"])


def downgrade() -> None:
    op.drop_index("ix_pecking_order_team_id", table_name="team_pecking_order")
    op.drop_table("team_pecking_order")
