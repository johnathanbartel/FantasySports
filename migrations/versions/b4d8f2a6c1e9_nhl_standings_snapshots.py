"""nhl_standings_snapshots

NHL standings points per team per calendar date, for Best Matchup (docs/hockey_spec.md 5.2).

Revision ID: b4d8f2a6c1e9
Revises: f9c2d5b8a1e4
Create Date: 2026-10-14 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4d8f2a6c1e9"
down_revision: Union[str, Sequence[str], None] = "f9c2d5b8a1e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "nhl_standings_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("universe_id", sa.Integer(), nullable=False),
        sa.Column("real_team_id", sa.Integer(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("games_played", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fetched_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["universe_id"], ["universes.id"]),
        sa.ForeignKeyConstraint(["real_team_id"], ["real_teams.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("universe_id", "real_team_id", "as_of_date", name="uq_nhl_standings_team_date"),
    )
    op.create_index("ix_nhl_standings_snapshots_universe_id", "nhl_standings_snapshots", ["universe_id"])
    op.create_index("ix_nhl_standings_snapshots_as_of_date", "nhl_standings_snapshots", ["as_of_date"])


def downgrade() -> None:
    op.drop_index("ix_nhl_standings_snapshots_as_of_date", table_name="nhl_standings_snapshots")
    op.drop_index("ix_nhl_standings_snapshots_universe_id", table_name="nhl_standings_snapshots")
    op.drop_table("nhl_standings_snapshots")
