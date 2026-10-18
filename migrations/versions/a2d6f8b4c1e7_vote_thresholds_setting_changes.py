"""vote_thresholds_setting_changes

CFG-06 policy: leagues.trade_vote_extra_votes (default 1: teams // 2 + 1 approvals) and
leagues.commissioner_trade_vote_extra_votes (default 2: when the commissioner's team is in the trade), plus the
league_setting_changes / league_setting_votes tables for unanimous midseason league_type / league_format changes.
Existing leagues get the defaults; no changes are pending.

Revision ID: a2d6f8b4c1e7
Revises: e4b8a1d6c3f9
Create Date: 2026-10-18 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a2d6f8b4c1e7"
down_revision: Union[str, Sequence[str], None] = "e4b8a1d6c3f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("trade_vote_extra_votes", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("commissioner_trade_vote_extra_votes", sa.Integer(), nullable=False, server_default="2"))
    op.create_table(
        "league_setting_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False, index=True),
        sa.Column("field", sa.String(20), nullable=False),
        sa.Column("old_value", sa.String(24), nullable=False),
        sa.Column("new_value", sa.String(24), nullable=False),
        sa.Column("proposed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("field IN ('league_type', 'league_format')", name="ck_setting_change_field"),
        sa.CheckConstraint("status IN ('voting', 'applied', 'rejected', 'cancelled')", name="ck_setting_change_status"),
    )
    op.create_table(
        "league_setting_votes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("change_id", sa.Integer(), sa.ForeignKey("league_setting_changes.id"), nullable=False, index=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("vote", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("change_id", "team_id", name="uq_setting_vote_team"),
        sa.CheckConstraint("vote IN ('approve', 'reject')", name="ck_setting_vote"),
    )


def downgrade() -> None:
    op.drop_table("league_setting_votes")
    op.drop_table("league_setting_changes")
    with op.batch_alter_table("leagues") as batch:
        batch.drop_column("commissioner_trade_vote_extra_votes")
        batch.drop_column("trade_vote_extra_votes")
