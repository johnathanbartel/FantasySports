"""platform_plans_trade_review

FS-036: leagues.platform_plan / platform_fee_cents / trade_review_hours / trade_review_mode, the platform_season_fees and
trade_vetoes tables, committed_by_trade_id on season_dues and platform_season_fees, and trades.review_ends_at.
Existing leagues become AD_SUPPORTED with a 24-hour two-way review window; no obligations are created.

Revision ID: e4b8a1d6c3f9
Revises: c7d1f3a9e5b2
Create Date: 2026-10-18 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e4b8a1d6c3f9"
down_revision: Union[str, Sequence[str], None] = "c7d1f3a9e5b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("platform_plan", sa.String(12), nullable=False, server_default="AD_SUPPORTED"))
        batch.add_column(sa.Column("platform_fee_cents", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("trade_review_hours", sa.Integer(), nullable=False, server_default="24"))
        batch.add_column(sa.Column("trade_review_mode", sa.String(12), nullable=False, server_default="both"))
        batch.create_check_constraint("ck_league_platform_plan", "platform_plan IN ('AD_SUPPORTED', 'AD_FREE')")
        batch.create_check_constraint("ck_league_trade_review_mode",
                                      "trade_review_mode IN ('none', 'commissioner', 'league', 'both')")
        batch.create_check_constraint("ck_league_trade_review_hours", "trade_review_hours BETWEEN 0 AND 72")
    with op.batch_alter_table("trades") as batch:
        batch.add_column(sa.Column("review_ends_at", sa.DateTime(), nullable=True))
    with op.batch_alter_table("season_dues") as batch:
        batch.add_column(sa.Column("committed_by_trade_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_season_dues_committed_trade", "trades", ["committed_by_trade_id"], ["id"])
    op.create_table(
        "platform_season_fees",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False, index=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False, index=True),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("required_cents", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("payment_reference", sa.String(120), nullable=False),
        sa.Column("recorded_by", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("committed_by_trade_id", sa.Integer(), sa.ForeignKey("trades.id"), nullable=True),
        sa.UniqueConstraint("league_id", "payment_reference", name="uq_platform_season_fees_reference"),
        sa.CheckConstraint("status IN ('pending', 'paid', 'failed', 'refunded', 'disputed')",
                           name="ck_platform_season_fees_status"),
    )
    op.create_table(
        "trade_vetoes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trade_id", sa.Integer(), sa.ForeignKey("trades.id"), nullable=False, index=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("trade_id", "team_id", name="uq_trade_veto_team"),
    )


def downgrade() -> None:
    op.drop_table("trade_vetoes")
    op.drop_table("platform_season_fees")
    with op.batch_alter_table("season_dues") as batch:
        batch.drop_constraint("fk_season_dues_committed_trade", type_="foreignkey")
        batch.drop_column("committed_by_trade_id")
    with op.batch_alter_table("trades") as batch:
        batch.drop_column("review_ends_at")
    with op.batch_alter_table("leagues") as batch:
        batch.drop_constraint("ck_league_trade_review_hours", type_="check")
        batch.drop_constraint("ck_league_trade_review_mode", type_="check")
        batch.drop_constraint("ck_league_platform_plan", type_="check")
        batch.drop_column("trade_review_mode")
        batch.drop_column("trade_review_hours")
        batch.drop_column("platform_fee_cents")
        batch.drop_column("platform_plan")
