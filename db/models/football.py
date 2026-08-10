"""
Football-specific schema: raw play-by-play ingestion and the EPA credit-
attribution output built on top of it. Not sport-agnostic - baseball's
locked-rotation mechanic doesn't have "plays" or "EPA" in this sense, so
this lives separately from db/models/core.py rather than forcing a shared
shape that wouldn't actually generalize.

`plays` mirrors the CFBD Play fields already used in
Football/College/pipeline.py (down, distance, yardsToGoal, yardsGained,
playType, playText, ppa, offense, defense, gameId, driveId, playNumber),
so wiring real ingestion into this table later is a field mapping, not a
redesign. `play_credits` generalizes the `credits`/`defense_credits` dicts
that pipeline.py's PipelineResult already produces.
"""

from __future__ import annotations

import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Play(Base):
    __tablename__ = "plays"
    __table_args__ = (UniqueConstraint("source", "external_play_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"))
    source: Mapped[str] = mapped_column(String(20))  # "cfbd"/"nflfastR"
    external_play_id: Mapped[str] = mapped_column(String(60))

    drive_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    play_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    period: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clock: Mapped[str | None] = mapped_column(String(10), nullable=True)
    down: Mapped[int | None] = mapped_column(Integer, nullable=True)
    distance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yard_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yards_to_goal: Mapped[int | None] = mapped_column(Integer, nullable=True)

    offense_team_id: Mapped[int | None] = mapped_column(ForeignKey("real_teams.id"), nullable=True)
    defense_team_id: Mapped[int | None] = mapped_column(ForeignKey("real_teams.id"), nullable=True)

    play_type: Mapped[str] = mapped_column(String(60))
    play_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    yards_gained: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ppa: Mapped[float | None] = mapped_column(Float, nullable=True)
    scoring: Mapped[bool] = mapped_column(default=False)

    ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())


class PlayCredit(Base):
    """One row per player/defense-group credited on a play. Generalizes
    PipelineResult.credits and .defense_credits from
    Football/College/pipeline.py - role distinguishes which side of a
    split this credit represents (e.g. a completed pass produces one
    'passer' row and one 'receiver' row, both pointing at the same play)."""

    __tablename__ = "play_credits"

    id: Mapped[int] = mapped_column(primary_key=True)
    play_id: Mapped[int] = mapped_column(ForeignKey("plays.id"))
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    role: Mapped[str] = mapped_column(String(20))  # passer/receiver/rusher/dl/lb/db/kicker
    credit_points: Mapped[float] = mapped_column(Float)


class WeeklyPlayerScore(Base):
    """Materialized aggregate of play_credits for fast lineup/scoring
    reads. play_credits stays the source of truth; this is a recomputable
    cache, not independently authoritative."""

    __tablename__ = "weekly_player_scores"
    __table_args__ = (UniqueConstraint("draftable_asset_id", "week_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    week_id: Mapped[int] = mapped_column(ForeignKey("weeks.id"))
    points: Mapped[float] = mapped_column(Float)
    computed_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())


class WeeklyRealTeamEpaTotal(Base):
    """Validation-only, not part of live fantasy scoring: persists exactly
    what Football/College/scripts/run_week_validation.py already computes
    ad hoc (model total vs. real points scored), so the spec's 'compare
    against reality' check has a permanent record instead of a one-off
    script run."""

    __tablename__ = "weekly_real_team_epa_totals"
    __table_args__ = (UniqueConstraint("real_team_id", "week_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    real_team_id: Mapped[int] = mapped_column(ForeignKey("real_teams.id"))
    week_id: Mapped[int] = mapped_column(ForeignKey("weeks.id"))
    model_total: Mapped[float] = mapped_column(Float)
    real_points_scored: Mapped[int | None] = mapped_column(Integer, nullable=True)
