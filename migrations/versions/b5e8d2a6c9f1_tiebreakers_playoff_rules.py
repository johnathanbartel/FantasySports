"""tiebreakers_playoff_rules

leagues.tiebreakers (the commissioner's tie-break order; NULL = the default order) and the playoff field rules used by the
mathematical elimination proof: byes, whether division winners are guaranteed a place, and whether they are seeded first.

Revision ID: b5e8d2a6c9f1
Revises: d3a9e5c1b7f4
Create Date: 2026-10-17 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b5e8d2a6c9f1"
down_revision: Union[str, Sequence[str], None] = "d3a9e5c1b7f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("tiebreakers", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("playoff_byes", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("division_winners_qualify", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("division_winners_seeded_first", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        for name in ("division_winners_seeded_first", "division_winners_qualify", "playoff_byes", "tiebreakers"):
            batch.drop_column(name)
