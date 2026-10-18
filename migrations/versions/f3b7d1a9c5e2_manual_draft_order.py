"""manual_draft_order

Manual draft order: leagues.draft_order_mode (random | manual; existing leagues get 'random', the old behaviour) and
teams.draft_position (a team's 1-based slot when the order is manual).

Revision ID: f3b7d1a9c5e2
Revises: e7a3c9b5d1f8
Create Date: 2026-10-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3b7d1a9c5e2"
down_revision: Union[str, Sequence[str], None] = "e7a3c9b5d1f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("draft_order_mode", sa.String(10), nullable=False, server_default="random"))
        batch.create_check_constraint("ck_league_draft_order_mode", "draft_order_mode IN ('random', 'manual')")
    with op.batch_alter_table("teams") as batch:
        batch.add_column(sa.Column("draft_position", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("teams") as batch:
        batch.drop_column("draft_position")
    with op.batch_alter_table("leagues") as batch:
        batch.drop_constraint("ck_league_draft_order_mode", type_="check")
        batch.drop_column("draft_order_mode")
