"""join_league

Public/private leagues, open human slots and the pre-draft slot policy (platform/league_join.py):
leagues.visibility, leagues.allow_ai_takeover_in_season, leagues.unfilled_slot_policy, teams.open_human_slot.
Existing leagues stay private with no open slots.

Revision ID: a4e7c2b9d1f6
Revises: a2d6f8b4c1e7
Create Date: 2026-10-18 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4e7c2b9d1f6"
down_revision: Union[str, Sequence[str], None] = "a2d6f8b4c1e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("visibility", sa.String(7), nullable=False, server_default="private"))
        batch.add_column(sa.Column("allow_ai_takeover_in_season", sa.Boolean(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("unfilled_slot_policy", sa.String(7), nullable=False, server_default="fill_ai"))
        batch.create_check_constraint("ck_league_visibility", "visibility IN ('public', 'private')")
        batch.create_check_constraint("ck_league_unfilled_slot_policy", "unfilled_slot_policy IN ('fill_ai', 'remove')")
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("open_human_slot", sa.Boolean(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("open_human_slot")
    with op.batch_alter_table("leagues") as batch:
        batch.drop_constraint("ck_league_unfilled_slot_policy", type_="check")
        batch.drop_constraint("ck_league_visibility", type_="check")
        batch.drop_column("unfilled_slot_policy")
        batch.drop_column("allow_ai_takeover_in_season")
        batch.drop_column("visibility")
