"""playoff_consolation

leagues.playoff_consolation: False (default) = an eliminated team's season ends at elimination; True = it plays consolation
matchups in the playoffs. Consolation games never count toward elimination.

Revision ID: c7d1f3a9e5b2
Revises: b5e8d2a6c9f1
Create Date: 2026-10-17 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7d1f3a9e5b2"
down_revision: Union[str, Sequence[str], None] = "b5e8d2a6c9f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.add_column(sa.Column("playoff_consolation", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("leagues") as batch:
        batch.drop_column("playoff_consolation")
