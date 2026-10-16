"""trade_hub_and_block

teams.trade_plan (Trade Hub: win_now / rebuilding, everyone starts Win Now) and the trade_block_entries table.

Revision ID: d3a9e5c1b7f4
Revises: c1f7a3e9d5b2
Create Date: 2026-10-16 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d3a9e5c1b7f4"
down_revision: Union[str, Sequence[str], None] = "c1f7a3e9d5b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("trade_plan", sa.String(12), nullable=False, server_default="win_now"))
    op.create_table(
        "trade_block_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False, index=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False, index=True),
        sa.Column("draftable_asset_id", sa.Integer(), sa.ForeignKey("draftable_assets.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("team_id", "draftable_asset_id", name="uq_trade_block_team_asset"),
    )


def downgrade() -> None:
    op.drop_table("trade_block_entries")
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("trade_plan")
