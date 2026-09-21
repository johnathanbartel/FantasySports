"""preseason schedule stage

Revision ID: a7d5f1c9e3b4
Revises: f6c4e0b8d2a3
Create Date: 2026-09-20 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7d5f1c9e3b4'
down_revision: Union[str, Sequence[str], None] = 'f6c4e0b8d2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "schedule_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("league_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="open"),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("league_id"),
    )
    op.create_table(
        "matchup_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("league_id", sa.Integer(), nullable=False),
        sa.Column("week_id", sa.Integer(), nullable=False),
        sa.Column("from_team_id", sa.Integer(), nullable=False),
        sa.Column("to_team_id", sa.Integer(), nullable=False),
        sa.Column("offer_amount", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.ForeignKeyConstraint(["week_id"], ["weeks.id"]),
        sa.ForeignKeyConstraint(["from_team_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["to_team_id"], ["teams.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_matchup_requests_league_id", "matchup_requests", ["league_id"])


def downgrade() -> None:
    op.drop_index("ix_matchup_requests_league_id", table_name="matchup_requests")
    op.drop_table("matchup_requests")
    op.drop_table("schedule_settings")
