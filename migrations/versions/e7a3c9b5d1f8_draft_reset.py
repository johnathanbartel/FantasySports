"""draft_reset

Draft reset: leagues.draft_reset_policy (unanimous | commissioner | off; existing leagues get 'unanimous') and the
draft_reset_requests / draft_reset_votes tables for resetting a completed draft by league vote.

Revision ID: e7a3c9b5d1f8
Revises: d1e5b9a3c7f2
Create Date: 2026-10-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7a3c9b5d1f8"
down_revision: Union[str, Sequence[str], None] = "d1e5b9a3c7f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("draft_reset_policy", sa.String(12), nullable=False, server_default="unanimous"))
        batch.create_check_constraint("ck_league_draft_reset_policy",
                                      "draft_reset_policy IN ('unanimous', 'commissioner', 'off')")
    op.create_table(
        "draft_reset_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False, index=True),
        sa.Column("draft_kind", sa.String(10), nullable=False),
        sa.Column("draft_id", sa.Integer(), nullable=False),
        sa.Column("keep", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("failure_reason", sa.String(300), nullable=True),
        sa.Column("proposed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("draft_kind IN ('snake', 'auction', 'recruiting')", name="ck_draft_reset_kind"),
        sa.CheckConstraint("status IN ('voting', 'applied', 'rejected', 'cancelled', 'failed')", name="ck_draft_reset_status"),
    )
    op.create_table(
        "draft_reset_votes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("draft_reset_requests.id"), nullable=False, index=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("vote", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("request_id", "team_id", name="uq_draft_reset_vote_team"),
        sa.CheckConstraint("vote IN ('approve', 'reject')", name="ck_draft_reset_vote"),
    )


def downgrade() -> None:
    op.drop_table("draft_reset_votes")
    op.drop_table("draft_reset_requests")
    with op.batch_alter_table("leagues") as batch:
        batch.drop_constraint("ck_league_draft_reset_policy", type_="check")
        batch.drop_column("draft_reset_policy")
