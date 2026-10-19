"""trade counter offers

trades.counter_of_trade_id: the offer a trade is a counter to (NULL for an ordinary proposal).

Revision ID: d2f6a8c4e1b7
Revises: c9e3a7b1d5f2
Create Date: 2026-10-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d2f6a8c4e1b7"
down_revision: Union[str, Sequence[str], None] = "c9e3a7b1d5f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("trades") as batch:
        batch.add_column(sa.Column("counter_of_trade_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_trades_counter_of_trade_id", "trades", ["counter_of_trade_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("trades") as batch:
        batch.drop_constraint("fk_trades_counter_of_trade_id", type_="foreignkey")
        batch.drop_column("counter_of_trade_id")
