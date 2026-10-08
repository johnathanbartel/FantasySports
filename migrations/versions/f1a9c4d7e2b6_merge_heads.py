"""merge heads

Joins the two migration branches (a3f7c1e9d2b4 league draft type/schedule, and
b8e2f4a6c1d7 team defense mode) so `alembic upgrade head` works again. Neither
side touches the other's tables, so the merge itself changes nothing.

Revision ID: f1a9c4d7e2b6
Revises: a3f7c1e9d2b4, b8e2f4a6c1d7
Create Date: 2026-10-08
"""
from typing import Sequence, Union

revision: str = 'f1a9c4d7e2b6'
down_revision: Union[str, Sequence[str], None] = ('a3f7c1e9d2b4', 'b8e2f4a6c1d7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
