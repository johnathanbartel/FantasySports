"""tradeable_picks

Draft picks in trades: traded_picks (who owns a pick that changed hands), pick columns on trade_items (whose asset id
becomes optional), and leagues.pick_trade_seasons.

Revision ID: f4d9b2e7a3c8
Revises: e3c8a1d5b9f2
Create Date: 2026-10-16 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4d9b2e7a3c8"
down_revision: Union[str, Sequence[str], None] = "e3c8a1d5b9f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("pick_trade_seasons", sa.Integer(), nullable=False, server_default="2"))
    with op.batch_alter_table("trade_items") as batch:
        batch.alter_column("draftable_asset_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("pick_season", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("pick_round", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("pick_original_team_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_trade_items_pick_original_team_id", "teams", ["pick_original_team_id"], ["id"])
    op.create_table(
        "traded_picks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("original_team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("owner_team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.UniqueConstraint("league_id", "season_number", "round", "original_team_id"),
    )
    op.create_index("ix_traded_picks_league_id", "traded_picks", ["league_id"])


def downgrade() -> None:
    op.drop_index("ix_traded_picks_league_id", table_name="traded_picks")
    op.drop_table("traded_picks")
    with op.batch_alter_table("trade_items") as batch:
        batch.drop_constraint("fk_trade_items_pick_original_team_id", type_="foreignkey")
        batch.drop_column("pick_original_team_id")
        batch.drop_column("pick_round")
        batch.drop_column("pick_season")
        batch.alter_column("draftable_asset_id", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table("leagues") as batch:
        batch.drop_column("pick_trade_seasons")
