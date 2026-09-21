"""user_sessions

Revision ID: e5b3d9a7c1f2
Revises: d4f2a8c1b6e9
Create Date: 2026-09-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5b3d9a7c1f2'
down_revision: Union[str, Sequence[str], None] = 'd4f2a8c1b6e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_sessions_token", "user_sessions", ["token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_user_sessions_token", table_name="user_sessions")
    op.drop_table("user_sessions")
