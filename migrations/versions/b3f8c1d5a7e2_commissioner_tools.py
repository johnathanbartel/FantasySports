"""commissioner_tools

Co-commissioners, per-team counter adjustments (transactions, FAAB), hand-set waiver orders, a commissioner action log,
an email outbox, and a note / edited-at on archived seasons.

Revision ID: b3f8c1d5a7e2
Revises: a8c3e5f1b7d2
Create Date: 2026-10-18 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b3f8c1d5a7e2"
down_revision: Union[str, Sequence[str], None] = "a8c3e5f1b7d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "league_co_commissioners",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("added_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("league_id", "user_id", name="uq_co_commissioner"),
    )
    op.create_index("ix_league_co_commissioners_league_id", "league_co_commissioners", ["league_id"])

    op.create_table(
        "team_adjustments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False, unique=True),
        sa.Column("transaction_adjustment", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("faab_adjustment", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_team_adjustments_league_id", "team_adjustments", ["league_id"])

    op.create_table(
        "waiver_order_overrides",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("team_order", sa.JSON(), nullable=False),
        sa.Column("set_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("set_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_waiver_order_overrides_league_id", "waiver_order_overrides", ["league_id"])

    op.create_table(
        "commissioner_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_commissioner_actions_league_id", "commissioner_actions", ["league_id"])

    op.create_table(
        "email_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("to_email", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(15), nullable=False, server_default="queued"),
        sa.Column("error", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_email_outbox_league_id", "email_outbox", ["league_id"])

    with op.batch_alter_table("league_seasons") as batch:
        batch.add_column(sa.Column("commissioner_note", sa.String(500), nullable=True))
        batch.add_column(sa.Column("edited_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("league_seasons") as batch:
        batch.drop_column("edited_at")
        batch.drop_column("commissioner_note")
    for table in ("email_outbox", "commissioner_actions", "waiver_order_overrides", "team_adjustments", "league_co_commissioners"):
        op.drop_table(table)
