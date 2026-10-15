"""window_rows

NHL window rows: series_daily_games.window_index / calendar_date_end (docs/hockey_spec.md sections 5 and 10.1).
Both nullable: existing one-row-per-day games keep working unchanged.

Revision ID: c7e1a9d3f5b2
Revises: b4d8f2a6c1e9
Create Date: 2026-10-14 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7e1a9d3f5b2"
down_revision: Union[str, Sequence[str], None] = "b4d8f2a6c1e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("series_daily_games") as batch:
        batch.add_column(sa.Column("calendar_date_end", sa.Date(), nullable=True))
        batch.add_column(sa.Column("window_index", sa.Integer(), nullable=True))
        batch.create_check_constraint("ck_series_daily_game_window_index", "window_index IS NULL OR window_index IN (1, 2, 3)")


def downgrade() -> None:
    with op.batch_alter_table("series_daily_games") as batch:
        batch.drop_constraint("ck_series_daily_game_window_index", type_="check")
        batch.drop_column("window_index")
        batch.drop_column("calendar_date_end")
