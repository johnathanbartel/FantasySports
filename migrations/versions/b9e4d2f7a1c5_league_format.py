"""league_format

leagues.league_format: how rosters carry over between seasons. Also merges the two open heads.

Revision ID: b9e4d2f7a1c5
Revises: a6d3f9b1c8e2, c3f7a1d9e5b4
Create Date: 2026-10-15 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b9e4d2f7a1c5"
down_revision: Union[str, Sequence[str], None] = ("a6d3f9b1c8e2", "c3f7a1d9e5b4")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(
            sa.Column("league_format", sa.String(24), nullable=False, server_default="traditional_redraft")
        )
        batch.create_check_constraint(
            "ck_league_format",
            "league_format IN ('traditional_redraft', 'future_pick_redraft', 'keeper', 'dynasty')",
        )
    op.execute(
        "UPDATE leagues SET league_format = 'future_pick_redraft' WHERE lower(name) = 'saints hockey league'"
    )


def downgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.drop_constraint("ck_league_format", type_="check")
        batch.drop_column("league_format")
