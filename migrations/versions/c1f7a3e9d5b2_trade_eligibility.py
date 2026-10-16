"""trade_eligibility

CFG-06: leagues.league_type / annual_dues_cents / trade_deadline, teams.eliminated_season, trades.failure_reason,
and the trade_approvals and season_dues tables. Existing leagues become FREE (no obligations are created).

Revision ID: c1f7a3e9d5b2
Revises: b6e3d8a2f1c4
Create Date: 2026-10-16 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1f7a3e9d5b2"
down_revision: Union[str, Sequence[str], None] = "b6e3d8a2f1c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("league_type", sa.String(4), nullable=False, server_default="FREE"))
        batch.add_column(sa.Column("annual_dues_cents", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("trade_deadline", sa.DateTime(), nullable=True))
        batch.create_check_constraint("ck_league_type", "league_type IN ('FREE', 'PAID')")
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("eliminated_season", sa.Integer(), nullable=True))
    with op.batch_alter_table("trades") as batch:
        batch.add_column(sa.Column("failure_reason", sa.String(500), nullable=True))
    op.create_table(
        "trade_approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trade_id", sa.Integer(), sa.ForeignKey("trades.id"), nullable=False, index=True),
        sa.Column("approval_type", sa.String(40), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("trade_id", "approval_type", name="uq_trade_approval_type"),
        sa.CheckConstraint("decision IN ('approve', 'reject')", name="ck_trade_approval_decision"),
    )
    op.create_table(
        "season_dues",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False, index=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False, index=True),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("required_cents", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("payment_reference", sa.String(120), nullable=False),
        sa.Column("recorded_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("league_id", "payment_reference", name="uq_season_dues_reference"),
        sa.CheckConstraint("status IN ('pending', 'paid', 'failed', 'refunded', 'disputed')", name="ck_season_dues_status"),
    )


def downgrade() -> None:
    op.drop_table("season_dues")
    op.drop_table("trade_approvals")
    with op.batch_alter_table("trades") as batch:
        batch.drop_column("failure_reason")
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("eliminated_season")
    with op.batch_alter_table("leagues") as batch:
        batch.drop_constraint("ck_league_type", type_="check")
        batch.drop_column("trade_deadline")
        batch.drop_column("annual_dues_cents")
        batch.drop_column("league_type")
