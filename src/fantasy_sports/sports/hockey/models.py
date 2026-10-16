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

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
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

    # Player credit targets (draftable assets); null when the source row names none.
    shooter_asset_id: Mapped[int | None] = mapped_column(ForeignKey("draftable_assets.id"), nullable=True)
    goalie_asset_id: Mapped[int | None] = mapped_column(ForeignKey("draftable_assets.id"), nullable=True)

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


class GameEvent(Base):
    """One NHL play-by-play event (FS-015): goals with their assists, shots,
    blocked shots, hits, giveaways, takeaways, faceoffs. Players are kept both
    as the source's player id (``*_ref``, always set when the source names
    someone) and as a draftable asset (null when the id is not in the roster
    files), so an assist by an unrostered player still counts as an assist.

    Actor columns by event type: goal = scorer / primary assist / secondary
    assist; blocked-shot = blocker / shooter; hit = hitter / hittee; faceoff =
    winner / loser; shot-on-goal, missed-shot = shooter; giveaway, takeaway =
    the player. ``team_id`` is the source's event-owning team."""

    __tablename__ = "game_events"
    __table_args__ = (UniqueConstraint("source", "external_event_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    source: Mapped[str] = mapped_column(String(20))  # "nhl_api"
    external_event_id: Mapped[str] = mapped_column(String(60))
    event_type: Mapped[str] = mapped_column(String(20))
    period: Mapped[int | None] = mapped_column(Integer, nullable=True)
    game_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)  # (period-1)*1200 + seconds into the period
    team_id: Mapped[int | None] = mapped_column(ForeignKey("real_teams.id"), nullable=True)

    actor_ref: Mapped[str | None] = mapped_column(String(20), nullable=True)
    secondary_ref: Mapped[str | None] = mapped_column(String(20), nullable=True)
    tertiary_ref: Mapped[str | None] = mapped_column(String(20), nullable=True)
    actor_asset_id: Mapped[int | None] = mapped_column(ForeignKey("draftable_assets.id"), nullable=True)
    secondary_asset_id: Mapped[int | None] = mapped_column(ForeignKey("draftable_assets.id"), nullable=True)
    tertiary_asset_id: Mapped[int | None] = mapped_column(ForeignKey("draftable_assets.id"), nullable=True)

    x_coord: Mapped[float | None] = mapped_column(Float, nullable=True)
    y_coord: Mapped[float | None] = mapped_column(Float, nullable=True)
    zone_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    shot_type: Mapped[str | None] = mapped_column(String(30), nullable=True)


class EventCredit(Base):
    """Credit on a play-by-play event rather than a shot row: blocks and hits
    (blocked shots are not in ``shots``), and assist credit when a game has
    events. role is one of blocker/hitter."""

    __tablename__ = "event_credits"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("game_events.id"), index=True)
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    role: Mapped[str] = mapped_column(String(20))
    credit_points: Mapped[float] = mapped_column(Float)


class NhlStandingsSnapshot(Base):
    """One team's NHL standings points as of a calendar date, fetched once from the NHL API and never overwritten.
    Best Matchup (docs/hockey_spec.md 5.2) reads the snapshot dated the day a window locks, so the selection made at
    lock can be reproduced later no matter how the standings move afterwards."""

    __tablename__ = "nhl_standings_snapshots"
    __table_args__ = (UniqueConstraint("universe_id", "real_team_id", "as_of_date", name="uq_nhl_standings_team_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    universe_id: Mapped[int] = mapped_column(ForeignKey("universes.id"), index=True)
    real_team_id: Mapped[int] = mapped_column(ForeignKey("real_teams.id"))
    as_of_date: Mapped[datetime.date] = mapped_column(Date, index=True)
    points: Mapped[int] = mapped_column(Integer)
    games_played: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())


class PlayerGameToi(Base):
    """Ice time of one dressed player in one real game, read from the NHL boxscore once the game is final. A game whose
    boxscore was stored has a row for every player who was dressed (a backup goalie who never played shows 0), and no row for
    a healthy scratch. So for a game with any row, "got no ice time" = no row, or a row with 0 seconds. A game with no rows has
    not had its boxscore stored: nothing is known about its players' ice time and nobody is treated as having sat out."""

    __tablename__ = "player_game_toi"
    __table_args__ = (UniqueConstraint("game_id", "draftable_asset_id", name="uq_player_game_toi"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    draftable_asset_id: Mapped[int] = mapped_column(ForeignKey("draftable_assets.id"))
    toi_seconds: Mapped[int] = mapped_column(Integer, default=0)
