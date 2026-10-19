"""chat

League chat, draft room chat and direct messages: chat_messages and chat_read_markers.

Revision ID: b2c6e9a4d8f1
Revises: a9d4f1c7e3b5
Create Date: 2026-10-22 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c6e9a4d8f1"
down_revision: Union[str, Sequence[str], None] = "a9d4f1c7e3b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("channel", sa.String(8), nullable=False),
        sa.Column("draft_id", sa.Integer(), sa.ForeignKey("drafts.id"), nullable=True),
        sa.Column("sender_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("recipient_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index("ix_chat_messages_league_id", "chat_messages", ["league_id"])
    op.create_index("ix_chat_thread", "chat_messages", ["league_id", "channel", "draft_id", "id"])
    op.create_index("ix_chat_dm", "chat_messages", ["league_id", "sender_user_id", "recipient_user_id"])
    op.create_table(
        "chat_read_markers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("league_id", sa.Integer(), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("thread_key", sa.String(24), nullable=False),
        sa.Column("last_read_id", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("user_id", "league_id", "thread_key", name="uq_chat_read"),
    )


def downgrade() -> None:
    op.drop_table("chat_read_markers")
    op.drop_index("ix_chat_dm", table_name="chat_messages")
    op.drop_index("ix_chat_thread", table_name="chat_messages")
    op.drop_index("ix_chat_messages_league_id", table_name="chat_messages")
    op.drop_table("chat_messages")
