"""simulator_published_periods

simulator_games.published_periods: NHL window games are published period by period (events are only appended).
NULL means the whole script was published at once.

Revision ID: d8a2b6e0c4f1
Revises: c7e1a9d3f5b2
Create Date: 2026-10-14 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8a2b6e0c4f1"
down_revision: Union[str, Sequence[str], None] = "c7e1a9d3f5b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("simulator_games", sa.Column("published_periods", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("simulator_games") as batch:
        batch.drop_column("published_periods")
