"""waiver period and processing days

leagues.waiver_hours: how long a dropped player is held on waivers (default 24, 0 = none).
leagues.waiver_processing_days: weekdays claims are processed on (NULL = when the hold ends).

Revision ID: f7b3c9d5e1a4
Revises: e6a2b8d4f1c3
Create Date: 2026-10-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f7b3c9d5e1a4"
down_revision: Union[str, Sequence[str], None] = "e6a2b8d4f1c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("waiver_hours", sa.Integer(), nullable=False, server_default="24"))
        batch.add_column(sa.Column("waiver_processing_days", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.drop_column("waiver_processing_days")
        batch.drop_column("waiver_hours")
