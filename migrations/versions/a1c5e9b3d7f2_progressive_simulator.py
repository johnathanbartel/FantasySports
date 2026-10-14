"""progressive_simulator

Published simulated games, their immutable events, and per-user reveal cursors (FS-025).

Revision ID: a1c5e9b3d7f2
Revises: e7a3c5d1f9b8
Create Date: 2026-10-10 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1c5e9b3d7f2"
down_revision: Union[str, Sequence[str], None] = "e7a3c5d1f9b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "simulator_games" not in tables:
        op.create_table(
        "simulator_games",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("daily_game_id", sa.Integer(), nullable=False),
        sa.Column("contract_version", sa.String(length=10), nullable=False),
        sa.Column("sport", sa.String(length=30), nullable=False),
        sa.Column("periods", sa.Integer(), nullable=False),
        sa.Column("period_seconds", sa.Integer(), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("script_digest", sa.String(length=64), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finalized_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("status IN ('SCHEDULED', 'LIVE', 'FINAL')", name="ck_simulator_game_status"),
        sa.CheckConstraint(
            "periods >= 1 AND period_seconds >= 1 AND window_seconds >= 1", name="ck_simulator_game_shape"
        ),
        sa.ForeignKeyConstraint(["daily_game_id"], ["series_daily_games.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("daily_game_id"),
    )
    if "simulator_events" not in tables:
        op.create_table(
        "simulator_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("simulator_game_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column("game_second", sa.Integer(), nullable=False),
        sa.Column("team_side", sa.String(length=4), nullable=True),
        sa.Column("home_score", sa.Integer(), nullable=False),
        sa.Column("away_score", sa.Integer(), nullable=False),
        sa.Column("home_shots", sa.Integer(), nullable=False),
        sa.Column("away_shots", sa.Integer(), nullable=False),
        sa.Column("headline", sa.String(length=200), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.CheckConstraint("seq >= 1", name="ck_simulator_event_seq"),
        sa.CheckConstraint(
            "home_score >= 0 AND away_score >= 0 AND home_shots >= 0 AND away_shots >= 0",
            name="ck_simulator_event_totals",
        ),
        sa.CheckConstraint("team_side IS NULL OR team_side IN ('home', 'away')", name="ck_simulator_event_side"),
        sa.ForeignKeyConstraint(["simulator_game_id"], ["simulator_games.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("simulator_game_id", "seq", name="uq_simulator_event_seq"),
    )
    if "simulator_reveals" not in tables:
        op.create_table(
        "simulator_reveals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("simulator_game_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=10), nullable=False),
        sa.Column("cursor_seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("mode IN ('WATCHING', 'SKIPPED')", name="ck_simulator_reveal_mode"),
        sa.CheckConstraint("cursor_seq >= 0", name="ck_simulator_reveal_cursor"),
        sa.ForeignKeyConstraint(["simulator_game_id"], ["simulator_games.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("simulator_game_id", "user_id", name="uq_simulator_reveal_game_user"),
    )


def downgrade() -> None:
    op.drop_table("simulator_reveals")
    op.drop_table("simulator_events")
    op.drop_table("simulator_games")
