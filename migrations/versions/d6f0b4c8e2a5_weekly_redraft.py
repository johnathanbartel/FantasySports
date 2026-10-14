"""weekly_redraft

Weekly salary-cap redraft mode: league_mode, settings, contests, pairings, salaries, entries and slots (FS-033).

Revision ID: d6f0b4c8e2a5
Revises: c5e9a3b7d1f4
Create Date: 2026-10-11 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d6f0b4c8e2a5"
down_revision: Union[str, Sequence[str], None] = "c5e9a3b7d1f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    league_cols = {c["name"] for c in inspector.get_columns("leagues")}

    if "league_mode" not in league_cols:
        with op.batch_alter_table("leagues") as batch_op:
            batch_op.add_column(sa.Column("league_mode", sa.String(length=20), nullable=False, server_default="standard"))
            batch_op.create_check_constraint("ck_league_mode", "league_mode IN ('standard', 'weekly_redraft')")

    if "weekly_redraft_settings" not in tables:
        op.create_table(
            "weekly_redraft_settings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("league_id", sa.Integer(), nullable=False),
            sa.Column("base_cap", sa.Integer(), nullable=False),
            sa.Column("cap_policy", sa.String(length=10), nullable=False, server_default="FIXED"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.CheckConstraint("base_cap > 0", name="ck_weekly_settings_cap"),
            sa.CheckConstraint("cap_policy IN ('FIXED')", name="ck_weekly_settings_policy"),
            sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("league_id"),
        )
    if "weekly_contests" not in tables:
        op.create_table(
            "weekly_contests",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("league_id", sa.Integer(), nullable=False),
            sa.Column("week_index", sa.Integer(), nullable=False),
            sa.Column("start_date", sa.Date(), nullable=False),
            sa.Column("end_date", sa.Date(), nullable=False),
            sa.Column("lock_at", sa.DateTime(), nullable=False),
            sa.Column("cap_applied", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=10), nullable=False, server_default="OPEN"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.CheckConstraint("end_date >= start_date", name="ck_weekly_contest_dates"),
            sa.CheckConstraint("cap_applied > 0", name="ck_weekly_contest_cap"),
            sa.CheckConstraint("status IN ('OPEN', 'LOCKED')", name="ck_weekly_contest_status"),
            sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("league_id", "week_index", name="uq_weekly_contest_week"),
            sa.UniqueConstraint("league_id", "start_date", name="uq_weekly_contest_start"),
        )
    if "weekly_contest_pairings" not in tables:
        op.create_table(
            "weekly_contest_pairings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("contest_id", sa.Integer(), nullable=False),
            sa.Column("team_a_id", sa.Integer(), nullable=False),
            sa.Column("team_b_id", sa.Integer(), nullable=False),
            sa.CheckConstraint("team_a_id <> team_b_id", name="ck_weekly_pairing_distinct"),
            sa.ForeignKeyConstraint(["contest_id"], ["weekly_contests.id"]),
            sa.ForeignKeyConstraint(["team_a_id"], ["teams.id"]),
            sa.ForeignKeyConstraint(["team_b_id"], ["teams.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("contest_id", "team_a_id", name="uq_weekly_pairing_a"),
            sa.UniqueConstraint("contest_id", "team_b_id", name="uq_weekly_pairing_b"),
        )
    if "weekly_player_salaries" not in tables:
        op.create_table(
            "weekly_player_salaries",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("contest_id", sa.Integer(), nullable=False),
            sa.Column("draftable_asset_id", sa.Integer(), nullable=False),
            sa.Column("salary", sa.Integer(), nullable=False),
            sa.CheckConstraint("salary > 0", name="ck_weekly_salary_positive"),
            sa.ForeignKeyConstraint(["contest_id"], ["weekly_contests.id"]),
            sa.ForeignKeyConstraint(["draftable_asset_id"], ["draftable_assets.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("contest_id", "draftable_asset_id", name="uq_weekly_salary_asset"),
        )
    if "weekly_contest_entries" not in tables:
        op.create_table(
            "weekly_contest_entries",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("contest_id", sa.Integer(), nullable=False),
            sa.Column("team_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=10), nullable=False, server_default="DRAFT"),
            sa.Column("salary_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cap", sa.Integer(), nullable=False),
            sa.Column("submitted_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.CheckConstraint("status IN ('DRAFT', 'SUBMITTED')", name="ck_weekly_entry_status"),
            sa.CheckConstraint("salary_total >= 0 AND salary_total <= cap", name="ck_weekly_entry_cap"),
            sa.ForeignKeyConstraint(["contest_id"], ["weekly_contests.id"]),
            sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("contest_id", "team_id", name="uq_weekly_entry_team"),
        )
    if "weekly_contest_slots" not in tables:
        op.create_table(
            "weekly_contest_slots",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("entry_id", sa.Integer(), nullable=False),
            sa.Column("slot_code", sa.String(length=2), nullable=False),
            sa.Column("slot_index", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("draftable_asset_id", sa.Integer(), nullable=False),
            sa.Column("salary", sa.Integer(), nullable=False),
            sa.CheckConstraint("salary > 0", name="ck_weekly_slot_salary"),
            sa.ForeignKeyConstraint(["entry_id"], ["weekly_contest_entries.id"]),
            sa.ForeignKeyConstraint(["draftable_asset_id"], ["draftable_assets.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("entry_id", "slot_code", "slot_index", name="uq_weekly_slot_position"),
            sa.UniqueConstraint("entry_id", "draftable_asset_id", name="uq_weekly_slot_asset"),
        )


def downgrade() -> None:
    for table in ("weekly_contest_slots", "weekly_contest_entries", "weekly_player_salaries", "weekly_contest_pairings",
                  "weekly_contests", "weekly_redraft_settings"):
        op.drop_table(table)
    with op.batch_alter_table("leagues") as batch_op:
        batch_op.drop_constraint("ck_league_mode", type_="check")
        batch_op.drop_column("league_mode")
