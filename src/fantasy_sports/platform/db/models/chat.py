"""
Chat tables (platform/chat.py): one message table for the league room, a draft's room and direct messages, plus a
per-reader marker for unread counts. A removed message keeps its row (deleted_at set) so the thread stays in order.
"""

from __future__ import annotations

import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from fantasy_sports.platform.db.base import Base


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_thread", "league_id", "channel", "draft_id", "id"),
        Index("ix_chat_dm", "league_id", "sender_user_id", "recipient_user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), index=True)
    channel: Mapped[str] = mapped_column(String(8))                       # league | draft | dm
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("drafts.id"), nullable=True)   # draft channel only
    sender_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    recipient_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)   # dm channel only
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class ChatReadMarker(Base):
    """The newest message a user has seen in one thread. thread_key: 'league', 'draft:<id>' or 'dm:<other user id>'."""

    __tablename__ = "chat_read_markers"
    __table_args__ = (UniqueConstraint("user_id", "league_id", "thread_key", name="uq_chat_read"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"))
    thread_key: Mapped[str] = mapped_column(String(24))
    last_read_id: Mapped[int] = mapped_column(Integer, default=0)
