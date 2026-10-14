"""draft_pick_clock

Per-pick clock for snake drafts: live seconds or slow/asynchronous hours, commissioner pause, overnight pause
windows and a server-side deadline (FS-030). All new columns are nullable or defaulted, so existing drafts keep
their untimed behaviour.

Revision ID: c5e9a3b7d1f4
Revises: b4d8f2a6c0e1
Create Date: 2026-10-10 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c5e9a3b7d1f4"
down_revision: Union[str, Sequence[str], None] = "b4d8f2a6c0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_cols = {c["name"] for c in sa.inspect(bind).get_columns("drafts")}
    cols = [
        ("pick_seconds", sa.Column("pick_seconds", sa.Integer(), nullable=True)),
        ("overnight_start_minute", sa.Column("overnight_start_minute", sa.Integer(), nullable=True)),
        ("overnight_end_minute", sa.Column("overnight_end_minute", sa.Integer(), nullable=True)),
        ("utc_offset_minutes", sa.Column("utc_offset_minutes", sa.Integer(), nullable=False, server_default="0")),
        ("paused", sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false())),
        ("paused_at", sa.Column("paused_at", sa.DateTime(), nullable=True)),
        ("remaining_seconds", sa.Column("remaining_seconds", sa.Integer(), nullable=True)),
        ("pick_started_at", sa.Column("pick_started_at", sa.DateTime(), nullable=True)),
        ("deadline_at", sa.Column("deadline_at", sa.DateTime(), nullable=True)),
    ]
    for name, col in cols:
        if name not in existing_cols:
            op.add_column("drafts", col)


def downgrade() -> None:
    for col in ("deadline_at", "pick_started_at", "remaining_seconds", "paused_at", "paused", "utc_offset_minutes",
                "overnight_end_minute", "overnight_start_minute", "pick_seconds"):
        op.drop_column("drafts", col)
