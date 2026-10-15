"""season_rollover

League season number / offseason state, per-draft season and snake flag, and the league_seasons archive.

Revision ID: c5a8e1f3d9b7
Revises: b9e4d2f7a1c5
Create Date: 2026-10-15 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c5a8e1f3d9b7"
down_revision: Union[str, Sequence[str], None] = "b9e4d2f7a1c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("season_number", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("offseason", sa.Boolean(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("offseason_draft_rounds", sa.Integer(), nullable=True))
    with op.batch_alter_table("drafts") as batch:
        batch.add_column(sa.Column("season_number", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("is_snake", sa.Boolean(), nullable=False, server_default="1"))
    op.create_table(
        "league_seasons",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("league_format", sa.String(24), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.UniqueConstraint("league_id", "season_number"),
    )
    op.create_index("ix_league_seasons_league_id", "league_seasons", ["league_id"])


def downgrade() -> None:
    op.drop_index("ix_league_seasons_league_id", table_name="league_seasons")
    op.drop_table("league_seasons")
    with op.batch_alter_table("drafts") as batch:
        batch.drop_column("is_snake")
        batch.drop_column("season_number")
    with op.batch_alter_table("leagues") as batch:
        batch.drop_column("offseason_draft_rounds")
        batch.drop_column("offseason")
        batch.drop_column("season_number")
