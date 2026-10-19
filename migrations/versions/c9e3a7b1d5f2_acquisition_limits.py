"""acquisition limits

leagues.acquisition_limit: the most players a team may acquire in a period (NULL = no limit).
leagues.acquisition_limit_period: day / week / game / series / season.

Revision ID: c9e3a7b1d5f2
Revises: b8d2f6a4c9e3
Create Date: 2026-10-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9e3a7b1d5f2"
down_revision: Union[str, Sequence[str], None] = "b8d2f6a4c9e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("acquisition_limit", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("acquisition_limit_period", sa.String(8), nullable=False, server_default="week"))


def downgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.drop_column("acquisition_limit_period")
        batch.drop_column("acquisition_limit")
