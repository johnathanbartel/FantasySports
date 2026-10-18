"""offline_draft_phones

Offline Draft Mode phase 3: PIN lock and pick queue on team claims, paused-time and one-open-lot index on auction lots.

Revision ID: d1e5b9a3c7f2
Revises: c8a4e2f6b1d9
Create Date: 2026-10-19 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1e5b9a3c7f2"
down_revision: Union[str, Sequence[str], None] = "c8a4e2f6b1d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("offline_team_claims") as b:
        b.add_column(sa.Column("locked", sa.Boolean(), nullable=False, server_default="0"))
        b.add_column(sa.Column("queue_json", sa.JSON(), nullable=True))
        b.add_column(sa.Column("claimed_at", sa.DateTime(), nullable=True))
    with op.batch_alter_table("offline_auction_lots") as b:
        b.add_column(sa.Column("paused_remaining", sa.Float(), nullable=True))
        b.add_column(sa.Column("created_at", sa.DateTime(), nullable=True))
    open_ = sa.text("status = 'open'")
    op.create_index("uq_offline_open_lot", "offline_auction_lots", ["session_id"], unique=True,
                    sqlite_where=open_, postgresql_where=open_)


def downgrade() -> None:
    op.drop_index("uq_offline_open_lot", table_name="offline_auction_lots")
    with op.batch_alter_table("offline_auction_lots") as b:
        b.drop_column("created_at")
        b.drop_column("paused_remaining")
    with op.batch_alter_table("offline_team_claims") as b:
        b.drop_column("claimed_at")
        b.drop_column("queue_json")
        b.drop_column("locked")
