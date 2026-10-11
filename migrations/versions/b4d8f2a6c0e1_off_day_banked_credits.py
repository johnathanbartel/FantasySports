"""off_day_banked_credits

Banked NHL off-day player credits (FS-028).

Revision ID: b4d8f2a6c0e1
Revises: a1c5e9b3d7f2
Create Date: 2026-10-10 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4d8f2a6c0e1"
down_revision: Union[str, Sequence[str], None] = "a1c5e9b3d7f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "off_day_banked_credits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("league_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("draftable_asset_id", sa.Integer(), nullable=False),
        sa.Column("source_calendar_date", sa.Date(), nullable=False),
        sa.Column("source_key", sa.String(length=120), nullable=False),
        sa.Column("credit_points", sa.Float(), nullable=False),
        sa.Column("applied_points", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("status_reason", sa.String(length=40), nullable=True),
        sa.Column("assigned_daily_game_id", sa.Integer(), nullable=True),
        sa.Column("assignment_mode", sa.String(length=10), nullable=True),
        sa.Column("assigned_at", sa.DateTime(), nullable=True),
        sa.Column("expired_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("credit_points > 0", name="ck_banked_credit_points"),
        sa.CheckConstraint(
            "applied_points IS NULL OR (applied_points > 0 AND applied_points <= 1.5)",
            name="ck_banked_credit_applied",
        ),
        sa.CheckConstraint("status IN ('AVAILABLE', 'ASSIGNED', 'EXPIRED')", name="ck_banked_credit_status"),
        sa.CheckConstraint(
            "assignment_mode IS NULL OR assignment_mode IN ('MANUAL', 'AUTO')", name="ck_banked_credit_mode"
        ),
        sa.CheckConstraint(
            "(status = 'ASSIGNED' AND assigned_daily_game_id IS NOT NULL AND applied_points IS NOT NULL) OR "
            "(status <> 'ASSIGNED' AND assigned_daily_game_id IS NULL AND applied_points IS NULL)",
            name="ck_banked_credit_assignment",
        ),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["draftable_asset_id"], ["draftable_assets.id"]),
        sa.ForeignKeyConstraint(["assigned_daily_game_id"], ["series_daily_games.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("league_id", "source_key", name="uq_banked_credit_source"),
        sa.UniqueConstraint("assigned_daily_game_id", "team_id", name="uq_banked_credit_game_team"),
    )
    op.create_index("ix_banked_credit_team_status", "off_day_banked_credits", ["team_id", "status"])
    op.create_index("ix_banked_credit_league_status", "off_day_banked_credits", ["league_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_banked_credit_league_status", table_name="off_day_banked_credits")
    op.drop_index("ix_banked_credit_team_status", table_name="off_day_banked_credits")
    op.drop_table("off_day_banked_credits")
