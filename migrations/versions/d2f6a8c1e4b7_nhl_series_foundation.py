"""nhl_series_foundation

Sport-agnostic persistence for NHL multi-day series and daily games.

Revision ID: d2f6a8c1e4b7
Revises: b7c3e9a1d4f8
Create Date: 2026-10-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d2f6a8c1e4b7"
down_revision: Union[str, Sequence[str], None] = "b7c3e9a1d4f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the NHL league setting and series persistence."""
    with op.batch_alter_table("leagues") as batch_op:
        batch_op.add_column(sa.Column("series_length_days", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            "ck_league_series_length_days",
            "series_length_days IS NULL OR series_length_days IN (5, 7, 10)",
        )

    # Existing NHL leagues adopt the approved default. Other leagues stay
    # null and therefore keep their existing weekly matchup behavior.
    op.execute(
        sa.text(
            "UPDATE leagues SET series_length_days = 7 "
            "WHERE id IN ("
            "SELECT lu.league_id FROM league_universes AS lu "
            "JOIN universes AS u ON u.id = lu.universe_id "
            "WHERE u.code = 'nhl'"
            ")"
        )
    )

    op.create_table(
        "league_series",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("league_id", sa.Integer(), nullable=False),
        sa.Column("team_a_id", sa.Integer(), nullable=False),
        sa.Column("team_b_id", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("total_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("team_a_wins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("team_b_wins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="SCHEDULED"),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.CheckConstraint("team_a_id <> team_b_id", name="ck_league_series_distinct_teams"),
        sa.CheckConstraint("end_date >= start_date", name="ck_league_series_date_order"),
        sa.CheckConstraint("total_days IN (5, 7, 10)", name="ck_league_series_total_days"),
        sa.CheckConstraint("team_a_wins >= 0", name="ck_league_series_team_a_wins"),
        sa.CheckConstraint("team_b_wins >= 0", name="ck_league_series_team_b_wins"),
        sa.CheckConstraint(
            "team_a_wins + team_b_wins <= total_days",
            name="ck_league_series_wins_within_total",
        ),
        sa.CheckConstraint(
            "status IN ('SCHEDULED', 'IN_PROGRESS', 'FINAL')",
            name="ck_league_series_status",
        ),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.ForeignKeyConstraint(["team_a_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["team_b_id"], ["teams.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "league_id", "team_a_id", "team_b_id", "start_date",
            name="uq_league_series_teams_start",
        ),
    )
    op.create_index(
        "ix_league_series_league_start_date", "league_series", ["league_id", "start_date"]
    )
    op.create_index("ix_league_series_team_a_id", "league_series", ["team_a_id"])
    op.create_index("ix_league_series_team_b_id", "league_series", ["team_b_id"])

    op.create_table(
        "series_daily_games",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("calendar_date", sa.Date(), nullable=False),
        sa.Column("is_matchup_day", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("game_score_rating", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("home_team_id", sa.Integer(), nullable=False),
        sa.Column("away_team_id", sa.Integer(), nullable=False),
        sa.Column("home_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("away_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_period", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("seconds_remaining", sa.Integer(), nullable=False, server_default="1200"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="SCHEDULED"),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.CheckConstraint(
            "home_team_id <> away_team_id", name="ck_series_daily_game_distinct_teams"
        ),
        sa.CheckConstraint(
            "game_score_rating BETWEEN 0 AND 100", name="ck_series_daily_game_score_rating"
        ),
        sa.CheckConstraint("home_score >= 0", name="ck_series_daily_game_home_score"),
        sa.CheckConstraint("away_score >= 0", name="ck_series_daily_game_away_score"),
        sa.CheckConstraint(
            "current_period BETWEEN 0 AND 3", name="ck_series_daily_game_current_period"
        ),
        sa.CheckConstraint(
            "seconds_remaining BETWEEN 0 AND 1200",
            name="ck_series_daily_game_seconds_remaining",
        ),
        sa.CheckConstraint(
            "status IN ('SCHEDULED', 'LIVE_SIMULATING', 'FINAL')",
            name="ck_series_daily_game_status",
        ),
        sa.ForeignKeyConstraint(["series_id"], ["league_series.id"]),
        sa.ForeignKeyConstraint(["home_team_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["away_team_id"], ["teams.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_id", "calendar_date", name="uq_series_daily_game_date"),
        sa.UniqueConstraint(
            "calendar_date", "home_team_id", "away_team_id",
            name="uq_series_daily_game_teams_date",
        ),
    )
    op.create_index(
        "ix_series_daily_games_calendar_status",
        "series_daily_games",
        ["calendar_date", "status"],
    )
    op.create_index(
        "ix_series_daily_games_home_team_id", "series_daily_games", ["home_team_id"]
    )
    op.create_index(
        "ix_series_daily_games_away_team_id", "series_daily_games", ["away_team_id"]
    )


def downgrade() -> None:
    """Remove series persistence and its league setting."""
    op.drop_index("ix_series_daily_games_away_team_id", table_name="series_daily_games")
    op.drop_index("ix_series_daily_games_home_team_id", table_name="series_daily_games")
    op.drop_index("ix_series_daily_games_calendar_status", table_name="series_daily_games")
    op.drop_table("series_daily_games")

    op.drop_index("ix_league_series_team_b_id", table_name="league_series")
    op.drop_index("ix_league_series_team_a_id", table_name="league_series")
    op.drop_index("ix_league_series_league_start_date", table_name="league_series")
    op.drop_table("league_series")

    with op.batch_alter_table("leagues") as batch_op:
        batch_op.drop_constraint("ck_league_series_length_days", type_="check")
        batch_op.drop_column("series_length_days")
