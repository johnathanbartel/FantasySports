"""keeper_offseason

Keeper / Dynasty offseason: league keeper settings, draft kinds, the rookie flag, keeper candidates and submissions.

Revision ID: d2b7f4a9c6e1
Revises: c5a8e1f3d9b7
Create Date: 2026-10-15 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d2b7f4a9c6e1"
down_revision: Union[str, Sequence[str], None] = "c5a8e1f3d9b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("keeper_draft_style", sa.String(16), nullable=False, server_default="rookie_resign"))
        batch.add_column(sa.Column("keeper_limit", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("keeper_deadline", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("keepers_finalized", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("offseason_refill_rounds", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_league_keeper_draft_style", "keeper_draft_style IN ('rookie_resign', 'combined', 'separate')"
        )
    with op.batch_alter_table("drafts") as batch:
        batch.add_column(sa.Column("kind", sa.String(10), nullable=False, server_default="standard"))
    with op.batch_alter_table("players") as batch:
        batch.add_column(sa.Column("is_rookie", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "keeper_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), sa.ForeignKey("draftable_assets.id"), nullable=False),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("games_played", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kept", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("roster_order", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("team_id", "draftable_asset_id", "season_number"),
    )
    op.create_index("ix_keeper_candidates_league_id", "keeper_candidates", ["league_id"])
    op.create_table(
        "keeper_submissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
        sa.Column("auto", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("team_id", "season_number"),
    )
    op.create_index("ix_keeper_submissions_league_id", "keeper_submissions", ["league_id"])


def downgrade() -> None:
    op.drop_index("ix_keeper_submissions_league_id", table_name="keeper_submissions")
    op.drop_table("keeper_submissions")
    op.drop_index("ix_keeper_candidates_league_id", table_name="keeper_candidates")
    op.drop_table("keeper_candidates")
    with op.batch_alter_table("players") as batch:
        batch.drop_column("is_rookie")
    with op.batch_alter_table("drafts") as batch:
        batch.drop_column("kind")
    with op.batch_alter_table("leagues") as batch:
        batch.drop_constraint("ck_league_keeper_draft_style", type_="check")
        for c in ("offseason_refill_rounds", "keepers_finalized", "keeper_deadline", "keeper_limit", "keeper_draft_style"):
            batch.drop_column(c)
