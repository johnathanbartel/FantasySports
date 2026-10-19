"""acquisition types

leagues.acquisition_type: no_waivers / waivers / full_waivers / budget.
leagues.acquisition_budget: the budget of a free "budget" league (a paid league uses its dues).
waiver_claims.bid: a claim's bid in a "budget" league.

Revision ID: b8d2f6a4c9e3
Revises: f7b3c9d5e1a4
Create Date: 2026-10-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8d2f6a4c9e3"
down_revision: Union[str, Sequence[str], None] = "f7b3c9d5e1a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("acquisition_type", sa.String(12), nullable=False, server_default="waivers"))
        batch.add_column(sa.Column("acquisition_budget", sa.Integer(), nullable=True))
    with op.batch_alter_table("waiver_claims") as batch:
        batch.add_column(sa.Column("bid", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("waiver_claims") as batch:
        batch.drop_column("bid")
    with op.batch_alter_table("leagues") as batch:
        batch.drop_column("acquisition_budget")
        batch.drop_column("acquisition_type")
