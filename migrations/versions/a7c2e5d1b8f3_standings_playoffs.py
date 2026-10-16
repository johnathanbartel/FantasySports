"""standings_playoffs

Hockey standings settings (points or win %, points per win / tie) and the number of playoff teams.

Revision ID: a7c2e5d1b8f3
Revises: f4d9b2e7a3c8
Create Date: 2026-10-16 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7c2e5d1b8f3"
down_revision: Union[str, Sequence[str], None] = "f4d9b2e7a3c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("standings_mode", sa.String(10), nullable=False, server_default="points"))
        batch.add_column(sa.Column("points_per_win", sa.Integer(), nullable=False, server_default="3"))
        batch.add_column(sa.Column("points_per_tie", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("playoff_teams", sa.Integer(), nullable=True))
        batch.create_check_constraint("ck_league_standings_mode", "standings_mode IN ('points', 'win_pct')")
    # Saints Hockey League: ranked by points, 3 for a win and 1 for a tie (the defaults, set explicitly).
    op.execute(
        "UPDATE leagues SET standings_mode = 'points', points_per_win = 3, points_per_tie = 1 "
        "WHERE lower(name) = 'saints hockey league'"
    )


def downgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.drop_constraint("ck_league_standings_mode", type_="check")
        for c in ("playoff_teams", "points_per_tie", "points_per_win", "standings_mode"):
            batch.drop_column(c)
