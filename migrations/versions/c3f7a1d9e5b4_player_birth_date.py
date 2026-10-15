"""player_birth_date

players.birth_date: date of birth (hockey: filled by the NHL roster sync), so the app can show age.

Revision ID: c3f7a1d9e5b4
Revises: b5c9e1a3d7f2
Create Date: 2026-10-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3f7a1d9e5b4"
down_revision: Union[str, Sequence[str], None] = "b5c9e1a3d7f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("players", sa.Column("birth_date", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("players") as batch:
        batch.drop_column("birth_date")
