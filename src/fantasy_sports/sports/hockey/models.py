"""
Hockey-specific schema: raw shot-event ingestion and the expected-goals (xG)
credit-attribution output built on top of it. Not sport-agnostic - a shot's
pre_shot_xg and strength_state don't generalize to football's down/distance
shape, so this lives separately from platform/db/models/core.py, the same
split the football module uses for its own play-level tables (see
sports/football/models.py's docstring).

`shots` is deliberately source-agnostic (NHL Stats API vs. MoneyPuck both
publish period/strength_state/shot_type/pre-shot xG in different field
names) so wiring up real ingestion later is a field mapping, not a redesign
- see the Data pipeline section of README.md. `shot_credits` generalizes the
shooter/primary_assist/secondary_assist/goalie split from the Credit
Attribution section of README.md, the same role-per-row shape as football's
play_credits.
"""

from __future__ import annotations

import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from fantasy_sports.platform.db.base import Base


class Shot(Base):
    __tablename__ = "shots"
    __table_args__ = (UniqueConstraint("source", "external_shot_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"))
    source: Mapped[str] = mapped_column(String(20))  # "nhl_api"/"moneypuck"
    external_shot_id: Mapped[str] = mapped_column(String(60))

    period: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clock: Mapped[str | None] = mapped_column(String(10), nullable=True)
    strength_state: Mapped[str | None] = mapped_column(String(10), nullable=True)  # "5v5"/"5v4"/"4v5"/...
    shot_type: Mapped[str | None] = mapped_column(String(30), nullable=True)  # "wrist"/"slap"/"tip-in"/...

    shooting_team_id: Mapped[int | None] = mapped_column(ForeignKey("real_teams.id"), nullable=True)
    goalie_team_id: Mapped[int | None] = mapped_column(ForeignKey("real_teams.id"), nullable=True)

    pre_shot_xg: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_goal: Mapped[bool] = mapped_column(Boolean, default=False)
    is_empty_net: Mapped[bool] = mapped_column(Boolean, default=False)

    ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())


class ShotCredit(Base):
    """One row per player credited on a shot - role distinguishes which
    share of the Credit Attribution split this row represents (a goal with
    two assists produces a shooter row, a primary_assist row, and a
    secondary_assist row, all pointing at the same shot; the goalie faced
    gets a separate goalie row on every shot, not just goals)."""

    __tablename__ = "shot_credits"

    id: Mapped[int] = mapped_column(primary_key=True)
    shot_id: Mapped[int] = mapped_column(ForeignKey("shots.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    role: Mapped[str] = mapped_column(String(20))  # shooter/primary_assist/secondary_assist/goalie
    credit_points: Mapped[float] = mapped_column(Float)
