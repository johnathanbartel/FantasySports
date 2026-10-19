"""mock_drafts

Mock drafts: leagues.is_mock_draft (a league that only runs one practice draft; existing leagues get false) and
leagues.mock_pick_seconds (the pick clock it starts with).

Revision ID: a9d4f1c7e3b5
Revises: f3b7d1a9c5e2
Create Date: 2026-10-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a9d4f1c7e3b5"
down_revision: Union[str, Sequence[str], None] = "f3b7d1a9c5e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("is_mock_draft", sa.Boolean(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("mock_pick_seconds", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.drop_column("mock_pick_seconds")
        batch.drop_column("is_mock_draft")
